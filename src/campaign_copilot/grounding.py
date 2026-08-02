"""Numeric grounding.

The rule: **every number in the final answer must be traceable to a tool result.**

This is not a hallucination *detector*. It is a hallucination *blocker*. Detection tells
you afterwards that you shipped a wrong number to a client. Blocking means the answer is
regenerated before anyone sees it. The eval harness reports `grounding_rate` with a
target of 1.00, which is only a meaningful target because the check is enforced, not
observed.

The hard part is not finding numbers in text; it is deciding what counts as a match.
An agent that queries `9.7013...` and writes "9.70" has not hallucinated, and a checker
that says otherwise will be switched off within a day. So two relaxations, each of
which is a rule rather than a fudge factor:

1. **Rounding.** A claim is grounded if some fact rounds to it at the claim's own
   precision. "9.70" is grounded by 9.7013 because ``round(9.7013, 2) == 9.70``.
2. **Scale.** "12.3%" is grounded by either 12.3 or 0.123. Ratios live in the warehouse
   as fractions and in prose as percentages, and the agent must be allowed to convert.
Everything else is ungrounded and the answer does not ship.

Question numbers are never evidence. The answer may say "the requested threshold" without
restating it, or a query may return an independently computed value. This conservative rule
closes the leading-question laundering path instead of trying to infer grammatical intent.

Spelled-out number phrases are rejected and regenerated in digits. The checker does not try to
convert ambiguous English into a value; it only needs to recognize that a numeric claim exists.
This closes the "nine point seven" bypass without adding a second arithmetic parser whose own
rounding and locale rules would need to be trusted.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

__all__ = ["GroundingChecker", "GroundingReport", "extract_numbers"]

#: Matches 1,234.56 / -0.5 / 9.70% / $1.2 / 2025.
#:
#: The comma-grouped form must require at least one group, or the alternation matches the
#: first three digits of a bare integer and reports "202" for the year 2025. The lookbehind
#: stops it firing inside identifiers such as `v2` or `paid_search_2`.
_NUMBER = re.compile(r"(?<![\w.])-?\$?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?")

#: Ordinals, small counts and years are prose, not claims. "the top 3 channels" is not
#: a number the warehouse needs to license.
_IGNORED_INTEGERS = frozenset(range(0, 13)) | frozenset(range(1990, 2101))

_NUMBER_WORDS = (
    "zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    "thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
    "twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|"
    "hundred|thousand|million|billion|trillion|point"
)
_SPELLED_NUMBER = re.compile(
    rf"\b(?:minus\s+|negative\s+)?(?:{_NUMBER_WORDS})"
    rf"(?:[ -]+(?:and[ -]+)?(?:{_NUMBER_WORDS}))*\b",
    re.IGNORECASE,
)
_IGNORED_SINGLE_WORDS = frozenset(
    {
        "zero",
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
        "eleven",
        "twelve",
    }
)


@dataclass(frozen=True, slots=True)
class Claim:
    """A number as it appeared in the answer."""

    raw: str
    value: Decimal
    is_percent: bool
    is_spelled: bool = False

    @property
    def decimals(self) -> int:
        """How precisely the claim was stated. Governs the rounding tolerance."""
        _, _, frac = str(self.value).partition(".")
        return len(frac)


def extract_numbers(text: str) -> list[Claim]:
    """Pull every numeric claim out of ``text``."""
    claims: list[Claim] = []
    for match in _NUMBER.finditer(text):
        raw = match.group()
        is_percent = raw.endswith("%")
        cleaned = raw.rstrip("%").replace(",", "").replace("$", "")
        try:
            value = Decimal(cleaned)
        except InvalidOperation:  # pragma: no cover - regex cannot produce this
            continue
        claims.append(Claim(raw=raw, value=value, is_percent=is_percent))
    return claims


def _extract_spelled_numbers(text: str) -> list[Claim]:
    """Find number words without pretending English is an exact numeric format."""
    claims: list[Claim] = []
    for match in _SPELLED_NUMBER.finditer(text):
        raw = match.group()
        if raw.lower() in _IGNORED_SINGLE_WORDS:
            continue
        claims.append(Claim(raw=raw, value=Decimal("NaN"), is_percent=False, is_spelled=True))
    return claims


@dataclass(frozen=True, slots=True)
class GroundingReport:
    """Whether an answer may be shown to the user."""

    ok: bool
    ungrounded: tuple[Claim, ...] = ()
    checked: int = 0
    #: Retained in the event contract. Strict grounding no longer emits question-only claims.
    context_only: tuple[Claim, ...] = ()

    def failure_message(self) -> str:
        """The message handed back to the model so it can regenerate the answer."""
        offenders = ", ".join(c.raw for c in self.ungrounded)
        return (
            f"The following numbers do not appear in any tool result: {offenders}. "
            "Every number you state must come from a query you ran. Remove them, or run "
            "the query that produces them, then answer again."
        )


@dataclass(slots=True)
class GroundingChecker:
    """Verifies that an answer states only numbers some tool produced."""

    relative_tolerance: Decimal = Decimal("0.0005")
    ignored_integers: frozenset[int] = field(default_factory=lambda: _IGNORED_INTEGERS)

    def check(
        self,
        answer: str,
        facts: list[float],
        *,
        context_numbers: list[float] | None = None,
        queries_run: bool = False,
    ) -> GroundingReport:
        """Report which claims in ``answer`` no fact supports.

        Args:
            answer: The text about to be sent to the user.
            facts: Every number any tool returned this turn.
            context_numbers: Retained for caller compatibility. Question numbers are never
                treated as evidence.
            queries_run: Retained for caller compatibility. Running an unrelated query does
                not license a number supplied by the user.
        """
        # A division by zero in the *agent's* query yields inf, and 0/0 yields nan. Both
        # arrive here as facts, and Decimal arithmetic on them raises InvalidOperation.
        # Dropping them is correct as well as safe: an infinite ROAS licenses no claim.
        from_tools = [Decimal(str(f)) for f in facts if math.isfinite(f)]
        del context_numbers, queries_run

        # The question is context, not evidence. Only finite tool facts enter the licensed set.
        ungrounded: list[Claim] = []
        claims = [*extract_numbers(answer), *_extract_spelled_numbers(answer)]
        for claim in claims:
            if self._is_ignorable(claim):
                continue
            if self._is_grounded(claim, from_tools):
                continue
            ungrounded.append(claim)

        return GroundingReport(
            ok=not ungrounded,
            ungrounded=tuple(ungrounded),
            checked=len(claims),
            context_only=(),
        )

    # ------------------------------------------------------------------ internals

    def _is_ignorable(self, claim: Claim) -> bool:
        """Small integers, years, and ordinals are prose."""
        if claim.is_spelled:
            return False
        if claim.decimals or claim.is_percent:
            return False
        try:
            return int(claim.value) in self.ignored_integers
        except (ValueError, OverflowError):  # pragma: no cover
            return False

    def _is_grounded(self, claim: Claim, licensed: list[Decimal]) -> bool:
        if claim.is_spelled:
            return False
        # (value, precision). Rescaling 3.8% to 0.038 also buys two decimal places of
        # precision: otherwise a claim of "3.8%" is grounded by a fact of 0.052, because
        # the tolerance was still one decimal place wide.
        candidates: list[tuple[Decimal, int]] = [(claim.value, claim.decimals)]
        if claim.is_percent:
            candidates.append((claim.value / Decimal(100), claim.decimals + 2))

        return any(
            self._matches(value, fact, decimals)
            for value, decimals in candidates
            for fact in licensed
        )

    def _matches(self, claim: Decimal, fact: Decimal, decimals: int) -> bool:
        if claim == fact:
            return True
        # Rounding: the claim's own precision sets the tolerance.
        quantum = Decimal(1).scaleb(-decimals)
        if abs(fact - claim) <= quantum / 2:
            return True
        # Relative tolerance, for values large enough that absolute rounding is silly.
        scale = abs(fact) if fact else Decimal(1)
        return abs(fact - claim) <= self.relative_tolerance * scale
