"""Semantic layer: the single source of truth for what a metric *means*.

The agent never writes ``sum(revenue_usd) / sum(spend_usd)`` by hand. It asks this
registry for ``roas`` and gets back a compiled expression. Two reasons, both of which
are the actual reason semantic layers exist in agency analytics:

1. **Ratio-of-sums, not average-of-ratios.** ``avg(daily_roas)`` and
   ``sum(revenue) / sum(spend)`` are different numbers, and the first one is wrong.
   An LLM asked to "average the ROAS by channel" will happily produce the wrong one.
   Here it cannot: ``roas`` compiles to a ratio of sums, always.

2. **Ambiguity is data, not documentation.** Each metric carries an ``ambiguity`` note.
   :meth:`SemanticLayer.ambiguity_notes` surfaces them so the agent can decide whether
   to answer or to ask. This feeds the ``clarification_precision`` eval metric directly.

Enforcement lives next door in :mod:`campaign_copilot.guardrails.sql_guard`, which
rejects any aggregate that is not an atom of some registered metric.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "Dimension",
    "Metric",
    "SemanticError",
    "SemanticLayer",
    "UnknownDimensionError",
    "UnknownMetricError",
]

DEFAULT_METRICS_PATH = Path(__file__).resolve().parents[3] / "semantic" / "metrics.yml"
DEFAULT_TABLE = "main_marts.campaign_performance_daily"


class SemanticError(ValueError):
    """Base class for semantic-layer failures."""


class UnknownMetricError(SemanticError):
    """Raised when a metric name is not registered.

    Carries a suggestion so the agent's repair loop has something to act on rather
    than retrying the same hallucinated name.
    """

    def __init__(self, name: str, known: Iterable[str]) -> None:
        """Attach close-match suggestions so the repair loop has something to act on."""
        close = difflib.get_close_matches(name, list(known), n=3, cutoff=0.5)
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        super().__init__(
            f"Unknown metric {name!r}. It is not defined in the semantic layer, and "
            f"the agent may not invent it.{hint}"
        )
        self.name = name
        self.suggestions = tuple(close)


class UnknownDimensionError(SemanticError):
    """Raised when a dimension name is not registered."""

    def __init__(self, name: str, known: Iterable[str]) -> None:
        """Build the error, attaching close-match suggestions for the repair loop."""
        close = difflib.get_close_matches(name, list(known), n=3, cutoff=0.5)
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        super().__init__(f"Unknown dimension {name!r}.{hint}")
        self.name = name
        self.suggestions = tuple(close)


@dataclass(frozen=True, slots=True)
class Metric:
    """One metric, defined exactly once."""

    name: str
    label: str
    description: str
    expression: str
    grain: tuple[str, ...]
    unit: str
    ambiguity: str | None = None

    @property
    def is_ratio(self) -> bool:
        """True when the metric divides one aggregate by another."""
        return self.unit == "ratio" or "/" in self.expression

    def select_sql(self) -> str:
        """Render the metric as an aliased SELECT item."""
        return f"{self.expression} as {self.name}"


@dataclass(frozen=True, slots=True)
class Dimension:
    """One groupable attribute."""

    name: str
    type: str
    description: str
    ambiguity: str | None = None


@dataclass(frozen=True, slots=True)
class Conventions:
    """House rules that the agent must state when it relies on them."""

    default_date_range: str = "last_28_days"
    currency: str = "USD"
    timezone: str = "UTC"
    fiscal_year_start: str = "01-01"
    rounding: str = ""


@dataclass
class SemanticLayer:
    """Loaded metric + dimension registry."""

    metrics: dict[str, Metric] = field(default_factory=dict)
    dimensions: dict[str, Dimension] = field(default_factory=dict)
    conventions: Conventions = field(default_factory=Conventions)

    # ---------------------------------------------------------------- loading

    @classmethod
    def load(cls, path: str | Path | None = None) -> SemanticLayer:
        """Parse ``metrics.yml`` into a registry of metrics, dimensions and conventions."""
        p = Path(path) if path is not None else DEFAULT_METRICS_PATH
        raw: Mapping[str, Any] = yaml.safe_load(p.read_text(encoding="utf-8"))

        metrics = {
            m["name"]: Metric(
                name=m["name"],
                label=m["label"],
                description=m["description"].strip(),
                expression=m["expression"].strip(),
                grain=tuple(m.get("grain") or ()),
                unit=m["unit"],
                ambiguity=(m.get("ambiguity") or None) and str(m["ambiguity"]).strip(),
            )
            for m in raw.get("metrics", [])
        }
        dimensions = {
            d["name"]: Dimension(
                name=d["name"],
                type=d["type"],
                description=str(d["description"]).strip(),
                ambiguity=(d.get("ambiguity") or None) and str(d["ambiguity"]).strip(),
            )
            for d in raw.get("dimensions", [])
        }
        conv = raw.get("conventions") or {}
        conventions = Conventions(
            default_date_range=conv.get("default_date_range", "last_28_days"),
            currency=conv.get("currency", "USD"),
            timezone=conv.get("timezone", "UTC"),
            fiscal_year_start=str(conv.get("fiscal_year_start", "01-01")),
            rounding=str(conv.get("rounding", "")).strip(),
        )
        return cls(metrics=metrics, dimensions=dimensions, conventions=conventions)

    # ---------------------------------------------------------------- lookup

    def metric(self, name: str) -> Metric:
        """Look up a metric, or raise :class:`UnknownMetricError`."""
        try:
            return self.metrics[name]
        except KeyError:
            raise UnknownMetricError(name, self.metrics) from None

    def dimension(self, name: str) -> Dimension:
        """Look up a dimension, or raise :class:`UnknownDimensionError`."""
        try:
            return self.dimensions[name]
        except KeyError:
            raise UnknownDimensionError(name, self.dimensions) from None

    def aggregate_atoms(self) -> frozenset[str]:
        """Every aggregate sub-expression that any registered metric is built from.

        This is the allowlist the SQL guardrail enforces. ``avg(roas)`` is not in it,
        which is precisely the point: an average of a ratio can never be produced.
        """
        import sqlglot
        from sqlglot import exp

        atoms: set[str] = set()
        for m in self.metrics.values():
            tree = sqlglot.parse_one(f"select {m.expression}", read="duckdb")
            for node in tree.find_all(exp.AggFunc):
                atoms.add(node.sql(dialect="duckdb").lower())
        return frozenset(atoms)

    # ---------------------------------------------------------------- ambiguity

    def ambiguity_notes(self, metrics: Sequence[str], filters: Sequence[str] = ()) -> list[str]:
        """Notes the agent must consider before answering.

        A non-empty result does not mean "refuse". It means "either disambiguate in the
        question, or state the assumption in the answer". The eval harness scores which
        of those two the agent chose.

        Dimension notes fire on *filters*, not on group-bys. Filtering a dimension hides
        a distinction; grouping by it displays one. "CTR by channel" needs no warning
        because brand and non-brand appear as separate rows. "CTR where channel like
        'paid_search%'" silently blends them, and does.
        """
        notes: list[str] = []
        for name in metrics:
            m = self.metric(name)
            if m.ambiguity:
                notes.append(f"[metric:{m.name}] {m.ambiguity}")

        filter_text = " ".join(filters).lower()
        for dim in self.dimensions.values():
            if dim.ambiguity and dim.name in filter_text:
                notes.append(f"[dimension:{dim.name}] {dim.ambiguity}")
        return notes

    # ---------------------------------------------------------------- compile

    def build_query(
        self,
        metrics: Sequence[str],
        dimensions: Sequence[str] = (),
        *,
        table: str = DEFAULT_TABLE,
        filters: Sequence[str] = (),
        order_by: str | None = None,
        descending: bool = True,
        limit: int | None = 100,
    ) -> str:
        """Compile a metric request into DuckDB SQL.

        Every metric is validated against the registry and every dimension against the
        metric's declared grain, so a request for ``blended_roas by campaign_name``
        fails loudly instead of returning a plausible, wrong number.
        """
        if not metrics:
            raise SemanticError("At least one metric is required.")

        resolved = [self.metric(m) for m in metrics]
        for d in dimensions:
            self.dimension(d)

        for m in resolved:
            illegal = [d for d in dimensions if m.grain and d not in m.grain]
            if illegal:
                raise SemanticError(
                    f"Metric {m.name!r} is not defined at grain {illegal!r}; "
                    f"its declared grain is {list(m.grain)}. "
                    "Refusing to compute it at a finer grain than it is defined."
                )

        select_parts = [*dimensions, *(m.select_sql() for m in resolved)]
        sql = f"select {', '.join(select_parts)}\nfrom {table}"

        if filters:
            sql += "\nwhere " + "\n  and ".join(f"({f})" for f in filters)
        if dimensions:
            sql += "\ngroup by " + ", ".join(str(i + 1) for i in range(len(dimensions)))
        if order_by:
            if order_by not in {*dimensions, *(m.name for m in resolved)}:
                raise SemanticError(
                    f"Cannot order by {order_by!r}: it is neither a selected metric "
                    "nor a selected dimension."
                )
            sql += f"\norder by {order_by} {'desc' if descending else 'asc'}"
        if limit is not None:
            sql += f"\nlimit {int(limit)}"
        return sql
