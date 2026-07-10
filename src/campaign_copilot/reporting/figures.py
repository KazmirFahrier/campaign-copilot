"""Figures: numbers that know where they came from.

The rule from the agent loop, applied to documents. Every number in a generated report is a
:class:`Figure`, and every Figure carries its provenance -- either the SQL that computed it,
or the arithmetic that derived it from other Figures. Nothing else may appear in the prose.

Two things fall out of that, and both are the point:

* :meth:`Figure.verify` re-executes the SQL and checks the value still matches. A report whose
  numbers cannot be reproduced from the warehouse it claims to describe is a document that has
  drifted, and it fails a test rather than a client meeting.
* The rendered document gets a provenance appendix. Every figure is footnoted `[F3]`, and `F3`
  resolves to the exact query. "Where did this number come from" is the first question anyone
  asks of an automated report, and it should not require reading the code.

A derived figure -- week-over-week delta, share of total -- has two parents and a formula
rather than a query. Its provenance is the chain, and `verify` recomputes it.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = ["FORMULAS", "Figure", "FigureSet", "ProvenanceError"]

#: The only arithmetic a derived figure may perform. A report generator that evaluates
#: expressions coming from a data file is a report generator with a remote code execution
#: bug, so this is a whitelist and there is no `eval` anywhere in this module.
FORMULAS: frozenset[str] = frozenset(
    {"pct_change", "abs_pct_change", "difference", "share_of_total"}
)


class ProvenanceError(RuntimeError):
    """A figure could not be reproduced from its stated provenance."""


@dataclass(frozen=True, slots=True)
class Figure:
    """One number, with the reason it is that number."""

    id: str
    label: str
    value: float
    unit: str = ""
    sql: str | None = None
    derived_from: tuple[str, ...] = ()
    formula: str = ""

    def __post_init__(self) -> None:
        """A figure without provenance is a claim, and claims are what we are avoiding."""
        if self.sql is None and not self.derived_from:
            raise ProvenanceError(f"Figure {self.id!r} has neither SQL nor parents.")
        if self.derived_from and not self.formula:
            raise ProvenanceError(f"Derived figure {self.id!r} has no formula.")
        if self.formula and self.formula not in FORMULAS:
            raise ProvenanceError(
                f"Figure {self.id!r} uses formula {self.formula!r}, which is not one of "
                f"{sorted(FORMULAS)}. Derived arithmetic is a whitelist, not an expression."
            )

    @property
    def is_derived(self) -> bool:
        """True when this figure was computed from other figures rather than from SQL."""
        return bool(self.derived_from)

    def render(self) -> str:
        """Format for prose: `$1,234.56`, `9.70x`, `3.8%`."""
        if self.unit == "usd":
            return f"${self.value:,.2f}"
        if self.unit == "ratio":
            return f"{self.value:.2f}x"
        if self.unit == "percent":
            return f"{self.value:.1f}%"
        if self.unit == "count":
            return f"{self.value:,.0f}"
        return f"{self.value:,.4f}"

    def cite(self) -> str:
        """The footnote marker that resolves to this figure's provenance."""
        return f"{self.render()} [{self.id}]"


@dataclass
class FigureSet:
    """Every figure in one document, indexed and verifiable."""

    figures: dict[str, Figure] = field(default_factory=dict)

    def add(self, figure: Figure) -> Figure:
        """Register a figure, rejecting duplicate ids and dangling parents."""
        if figure.id in self.figures:
            raise ProvenanceError(f"Duplicate figure id {figure.id!r}")
        for parent in figure.derived_from:
            if parent not in self.figures:
                raise ProvenanceError(
                    f"Figure {figure.id!r} derives from {parent!r}, which is not registered."
                )
        self.figures[figure.id] = figure
        return figure

    def __getitem__(self, key: str) -> Figure:
        """Look up by id."""
        return self.figures[key]

    def facts(self) -> list[float]:
        """Every value the narrative is licensed to state.

        This is what gets handed to :class:`~campaign_copilot.grounding.GroundingChecker`.
        A number in the prose that is not in this list does not ship, exactly as in the
        agent loop, using exactly the same checker.
        """
        return [f.value for f in self.figures.values() if math.isfinite(f.value)]

    def verify(
        self,
        execute: Callable[[str], Sequence[Sequence[Any]]],
        *,
        tolerance: float = 1e-6,
    ) -> list[str]:
        """Re-derive every figure and report the ones that no longer reproduce.

        ``execute`` runs a query and returns rows. Query figures are re-run; derived figures
        are recomputed from their parents. An empty list is the only acceptable result, and
        it is asserted in the test suite against the real warehouse.
        """
        problems: list[str] = []
        for figure in self.figures.values():
            if figure.sql is not None:
                rows = execute(figure.sql)
                if not rows or not rows[0]:
                    problems.append(f"{figure.id}: SQL returned no rows")
                    continue
                actual = rows[0][-1]
                if actual is None or not math.isclose(
                    float(actual), figure.value, rel_tol=tolerance, abs_tol=tolerance
                ):
                    problems.append(
                        f"{figure.id}: SQL yields {actual!r}, figure says {figure.value!r}"
                    )
            else:
                parents = [self.figures[p].value for p in figure.derived_from]
                expected = _recompute(figure.formula, parents)
                if expected is None:
                    problems.append(f"{figure.id}: unknown formula {figure.formula!r}")
                elif not math.isclose(expected, figure.value, rel_tol=1e-9, abs_tol=1e-9):
                    problems.append(
                        f"{figure.id}: formula yields {expected!r}, "
                        f"figure says {figure.value!r}"
                    )
        return problems

    def appendix(self) -> list[tuple[str, str, str]]:
        """`(id, label, provenance)` for every figure, for the document's footnotes."""
        rows: list[tuple[str, str, str]] = []
        for figure in self.figures.values():
            if figure.sql is not None:
                provenance = " ".join(figure.sql.split())
            else:
                parents = ", ".join(figure.derived_from)
                provenance = f"{figure.formula} of [{parents}]"
            rows.append((figure.id, figure.label, provenance))
        return rows


def _recompute(formula: str, parents: list[float]) -> float | None:
    """Evaluate one whitelisted formula. See :data:`FORMULAS`."""
    if formula == "pct_change" and len(parents) == 2:
        current, previous = parents
        if previous == 0:
            return math.inf if current else 0.0
        return (current - previous) / previous * 100.0
    if formula == "abs_pct_change" and len(parents) == 2:
        # The narrative says "down 12.3%", not "up -12.3%". The magnitude is the number that
        # appears in the prose, so the magnitude is what must be grounded. Direction is a
        # word, and words are not checked by the grounding gate.
        current, previous = parents
        if previous == 0:
            return math.inf if current else 0.0
        return abs((current - previous) / previous * 100.0)
    if formula == "difference" and len(parents) == 2:
        return parents[0] - parents[1]
    if formula == "share_of_total" and len(parents) == 2:
        part, total = parents
        return (part / total * 100.0) if total else 0.0
    return None
