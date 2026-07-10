"""The weekly campaign review.

Builds a document from the semantic layer, not from hand-written SQL. Every headline number
is a :class:`Figure` compiled by ``build_query``, so the report cannot disagree with the agent
about what ROAS means -- there is one definition and both read it.

The narrative then passes through :class:`~campaign_copilot.grounding.GroundingChecker`, the
same class the agent loop uses. A generated sentence that mentions a number no figure supports
raises rather than renders. This closes the last place a wrong number could reach a client:
the agent refuses to say one, and the report refuses to print one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from campaign_copilot.grounding import GroundingChecker
from campaign_copilot.reporting.figures import Figure, FigureSet, ProvenanceError
from campaign_copilot.semantic.layer import SemanticLayer
from campaign_copilot.tools.sql import Warehouse

__all__ = ["ChannelRow", "WeeklyReview", "build_weekly_review"]

#: The definitional decision the whole report rests on. Stated, not assumed.
CAMPAIGN_ONLY = "campaign_name <> '(direct)'"


@dataclass(frozen=True, slots=True)
class ChannelRow:
    """One channel's week."""

    channel: str
    spend: float
    revenue: float
    roas: float | None


@dataclass
class WeeklyReview:
    """A generated report, before it is rendered into any particular format."""

    week_start: date
    week_end: date
    figures: FigureSet
    channels: list[ChannelRow] = field(default_factory=list)
    daily: list[tuple[date, float, float]] = field(default_factory=list)
    narrative: str = ""

    @property
    def title(self) -> str:
        """Human title for the covered period."""
        return f"Campaign review: {self.week_start:%d %b} to {self.week_end:%d %b %Y}"


def _scalar(warehouse: Warehouse, sql: str) -> float | None:
    _, rows = warehouse.execute(sql)
    if not rows or rows[0][-1] is None:
        return None
    return float(rows[0][-1])


def _window(start: date, end: date) -> list[str]:
    return [f"event_date >= date '{start:%Y-%m-%d}'", f"event_date <= date '{end:%Y-%m-%d}'"]


def build_weekly_review(
    layer: SemanticLayer,
    warehouse: Warehouse,
    week_end: date,
    *,
    checker: GroundingChecker | None = None,
) -> WeeklyReview:
    """Compute every figure, then write a narrative that only cites those figures.

    Raises:
        ProvenanceError: if the narrative states a number no figure supports. That is a bug in
            the narrative template, and it should stop the build rather than reach a client.
    """
    week_start = week_end - timedelta(days=6)
    prior_end = week_start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=6)

    figures = FigureSet()

    # ---- headline figures, compiled through the semantic layer
    plans: list[tuple[str, str, str, str]] = [
        ("F1", "Spend, this week", "spend", "usd"),
        ("F2", "Revenue, this week", "revenue", "usd"),
        ("F3", "ROAS, this week", "roas", "ratio"),
        ("F4", "Cost per acquisition, this week", "cpa", "usd"),
    ]
    for fid, label, metric, unit in plans:
        sql = layer.build_query(
            [metric], filters=[*_window(week_start, week_end), CAMPAIGN_ONLY]
        )
        value = _scalar(warehouse, sql)
        if value is None:
            raise ProvenanceError(f"{metric} is undefined for the week ending {week_end}")
        figures.add(Figure(id=fid, label=label, value=value, unit=unit, sql=sql))

    for fid, label, metric in [
        ("F5", "Spend, prior week", "spend"),
        ("F6", "Revenue, prior week", "revenue"),
    ]:
        sql = layer.build_query(
            [metric], filters=[*_window(prior_start, prior_end), CAMPAIGN_ONLY]
        )
        value = _scalar(warehouse, sql)
        if value is None:
            raise ProvenanceError(f"{metric} is undefined for the prior week")
        figures.add(Figure(id=fid, label=label, value=value, unit="usd", sql=sql))

    figures.add(
        Figure(
            id="F7",
            label="Spend change vs prior week (magnitude)",
            value=abs(_pct(figures["F1"].value, figures["F5"].value)),
            unit="percent",
            derived_from=("F1", "F5"),
            formula="abs_pct_change",
        )
    )
    figures.add(
        Figure(
            id="F8",
            label="Revenue change vs prior week (magnitude)",
            value=abs(_pct(figures["F2"].value, figures["F6"].value)),
            unit="percent",
            derived_from=("F2", "F6"),
            formula="abs_pct_change",
        )
    )

    # ---- channel breakdown
    channel_sql = layer.build_query(
        ["spend", "revenue", "roas"],
        ["channel"],
        filters=[*_window(week_start, week_end), CAMPAIGN_ONLY],
        order_by="spend",
    )
    _, rows = warehouse.execute(channel_sql)
    channels = [ChannelRow(r[0], float(r[1]), float(r[2]), _opt(r[3])) for r in rows]

    # `roas` is Optional on the row; narrow it before it reaches `key`, or mypy is right to
    # complain that None has no ordering.
    rated = [(c, c.roas) for c in channels if c.roas is not None]
    best = max(rated, key=lambda pair: pair[1])[0] if rated else None
    if best is not None:
        figures.add(
            Figure(
                id="F9",
                label=f"ROAS, {best.channel}",
                value=best.roas if best.roas is not None else 0.0,
                unit="ratio",
                # No `["channel"]` group-by: the channel is already pinned by the filter, so
                # grouping added a second column and made a scalar figure's query non-scalar
                # (docs/AUDIT.md, R3-1, found by the stricter verify).
                sql=layer.build_query(
                    ["roas"],
                    filters=[
                        *_window(week_start, week_end),
                        CAMPAIGN_ONLY,
                        f"channel = '{best.channel}'",
                    ],
                ),
            )
        )

    # ---- daily series for the chart
    daily_sql = layer.build_query(
        ["spend", "revenue"],
        ["event_date"],
        filters=[*_window(week_start, week_end), CAMPAIGN_ONLY],
        order_by="event_date",
        descending=False,
    )
    _, daily_rows = warehouse.execute(daily_sql)
    daily = [(r[0], float(r[1]), float(r[2])) for r in daily_rows]

    review = WeeklyReview(
        week_start=week_start,
        week_end=week_end,
        figures=figures,
        channels=channels,
        daily=daily,
    )
    review.narrative = _narrative(review, best)

    # The same gate the agent uses, on the same numbers, with the same class.
    report = (checker or GroundingChecker()).check(
        review.narrative, figures.facts(), queries_run=True
    )
    if not report.ok:
        raise ProvenanceError(
            "The narrative states numbers no figure supports: "
            + ", ".join(c.raw for c in report.ungrounded)
        )
    return review


def _pct(current: float, previous: float) -> float:
    return (current - previous) / previous * 100.0 if previous else 0.0


def _opt(value: Any) -> float | None:
    return None if value is None else float(value)


def _narrative(review: WeeklyReview, best: ChannelRow | None) -> str:
    """Deterministic prose. Every number is a figure; there is nowhere else to get one.

    An LLM can write this instead -- the grounding gate above does not care who wrote it, and
    that is the whole argument for putting the gate there rather than in the prompt.
    """
    f = review.figures
    spend_word = "up" if f["F1"].value >= f["F5"].value else "down"
    revenue_word = "up" if f["F2"].value >= f["F6"].value else "down"

    parts = [
        f"Campaign spend was {f['F1'].cite()}, {spend_word} {f['F7'].cite()} "
        f"on the prior week, against revenue of {f['F2'].cite()}, "
        f"{revenue_word} {f['F8'].cite()}.",
        f"Blended campaign ROAS came in at {f['F3'].cite()} with a cost per acquisition of "
        f"{f['F4'].cite()}.",
    ]
    if best is not None:
        parts.append(
            f"{best.channel.replace('_', ' ')} led on efficiency at {f['F9'].cite()}, though "
            "branded search cannibalises organic demand and should not be read as incremental."
        )
    parts.append(
        "Unattributed ('direct') revenue is excluded throughout; including it would raise "
        "every ROAS figure without any corresponding media cost."
    )
    return " ".join(parts)
