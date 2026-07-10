"""Scoring primitives.

Nothing here talks to a model. These are the functions that decide whether two result sets
are the same answer, whether a tool sequence matches, and whether two graders agree. Each is
tested against hand-worked examples, because a bug in a metric is worse than a bug in the
system it measures: it is invisible and it points the wrong way.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from typing import Any

__all__ = ["cohens_kappa", "multiset_f1", "normalize_row", "result_set_match"]

DEFAULT_TOLERANCE = 1e-6


def normalize_row(
    row: Sequence[Any], *, tolerance: float = DEFAULT_TOLERANCE
) -> tuple[Any, ...]:
    """Round floats so that 9.700000000001 and 9.7 are the same answer.

    Rounding to the tolerance's order of magnitude, rather than comparing with `isclose`,
    lets rows be hashed and compared as multisets. Exact equality on floats produced by two
    different SQL expressions is a test that fails for reasons nobody learns anything from.
    """
    digits = max(0, -math.floor(math.log10(tolerance)))
    out: list[Any] = []
    for value in row:
        if isinstance(value, bool) or value is None:
            out.append(value)
        elif isinstance(value, (int, float)):
            out.append(round(float(value), digits))
        else:
            out.append(str(value))
    return tuple(out)


def result_set_match(
    candidate: Sequence[Sequence[Any]],
    gold: Sequence[Sequence[Any]],
    *,
    tolerance: float = DEFAULT_TOLERANCE,
) -> bool:
    """Compare result sets as multisets of rows, ignoring row order.

    Column *order* still matters and column *names* do not, which matches what a person
    means by "the same answer". An `ORDER BY` that the question did not ask for should not
    fail a case, and a query returning each row twice should.
    """
    if len(candidate) != len(gold):
        return False
    left = Counter(normalize_row(r, tolerance=tolerance) for r in candidate)
    right = Counter(normalize_row(r, tolerance=tolerance) for r in gold)
    return left == right


def multiset_f1(predicted: Sequence[str], gold: Sequence[str]) -> float:
    """F1 over tool names, counting repeats.

    A multiset, not a set: an agent that calls `run_sql` four times to answer a one-query
    question has done something different from one that called it once, and set-F1 would
    score them identically.
    """
    if not predicted and not gold:
        return 1.0
    if not predicted or not gold:
        return 0.0
    overlap = sum((Counter(predicted) & Counter(gold)).values())
    if not overlap:
        return 0.0
    precision = overlap / len(predicted)
    recall = overlap / len(gold)
    return 2 * precision * recall / (precision + recall)


def cohens_kappa(a: Sequence[bool], b: Sequence[bool]) -> float:
    """Chance-corrected agreement between two binary graders.

    Raw agreement is a vanity metric. If 90% of answers are correct, two graders that both
    say "correct" every time agree 90% of the time and have learned nothing. Kappa subtracts
    the agreement expected by chance:

        kappa = (p_observed - p_chance) / (1 - p_chance)

    Returns 1.0 for perfect agreement, 0.0 for chance-level, negative for worse than chance.
    By convention kappa >= 0.6 is "substantial"; below that an LLM judge should not be used
    as a stand-in for human labels, and the honest move is to say so rather than to report
    the judge's score anyway.
    """
    if len(a) != len(b):
        raise ValueError("graders must label the same number of items")
    if not a:
        raise ValueError("no items to compare")

    n = len(a)
    observed = sum(x == y for x, y in zip(a, b, strict=True)) / n

    p_a_true, p_b_true = sum(a) / n, sum(b) / n
    chance = p_a_true * p_b_true + (1 - p_a_true) * (1 - p_b_true)

    if math.isclose(chance, 1.0):
        # Both graders were constant and identical. Agreement is total and meaningless.
        return 1.0 if math.isclose(observed, 1.0) else 0.0
    return (observed - chance) / (1 - chance)
