"""Grounding tests.

The checker has to be permissive enough that a correct agent is never blocked, and strict
enough that an invented number always is. Both halves are tested; the second half is the
one that matters, the first is the one that keeps the check switched on.
"""

from __future__ import annotations

from campaign_copilot.grounding import GroundingChecker, extract_numbers

CHECKER = GroundingChecker()


# ---------------------------------------------------------------- extraction


def test_extraction_handles_currency_commas_and_percent() -> None:
    claims = extract_numbers("Spend was $1,234.56 and CTR was 3.8%.")
    values = [str(c.value) for c in claims]
    assert "1234.56" in values
    assert "3.8" in values
    assert any(c.is_percent for c in claims)


def test_negative_numbers_are_extracted() -> None:
    assert any(c.value < 0 for c in extract_numbers("Revenue fell by -12.5 points."))


# ------------------------------------------------------- the permissive half


def test_a_rounded_claim_is_grounded_by_the_full_precision_fact() -> None:
    """The agent queries 9.7013 and writes 9.70. That is correct behaviour."""
    report = CHECKER.check("ROAS was 9.70.", facts=[9.7013])
    assert report.ok


def test_rounding_tolerance_scales_with_the_stated_precision() -> None:
    assert CHECKER.check("ROAS was 9.7.", facts=[9.7013]).ok
    assert CHECKER.check("ROAS was 9.701.", facts=[9.7013]).ok


def test_a_percentage_is_grounded_by_the_underlying_fraction() -> None:
    """Ratios live in the warehouse as fractions and in prose as percentages."""
    assert CHECKER.check("CTR was 3.8%.", facts=[0.038]).ok


def test_a_percentage_is_also_grounded_by_the_percentage_itself() -> None:
    assert CHECKER.check("CTR was 3.8%.", facts=[3.8]).ok


def test_currency_formatting_does_not_break_grounding() -> None:
    assert CHECKER.check("Spend was $1,234.56.", facts=[1234.5612]).ok


def test_small_integers_and_years_are_prose_not_claims() -> None:
    report = CHECKER.check("The top 3 channels in 2025 were listed.", facts=[])
    assert report.ok
    assert report.checked == 2


def test_numbers_the_user_supplied_are_grounded_by_the_question() -> None:
    """A question that names a 2.5x threshold licenses the agent to repeat 2.5."""
    report = CHECKER.check("Two campaigns beat 2.5x ROAS.", facts=[], context_numbers=[2.5])
    assert report.ok


# ---------------------------------------------------------- the strict half


def test_an_invented_number_is_blocked() -> None:
    report = CHECKER.check("ROAS was 9.70 and spend was $412,000.", facts=[9.7013])
    assert not report.ok
    assert [c.raw for c in report.ungrounded] == ["$412,000"]


def test_a_plausible_but_wrong_number_is_blocked() -> None:
    """9.8 is close to 9.7013 and is still not what the query returned."""
    report = CHECKER.check("ROAS was 9.8.", facts=[9.7013])
    assert not report.ok


def test_the_failure_message_names_the_offenders_and_tells_the_model_what_to_do() -> None:
    report = CHECKER.check("Spend was $412,000.", facts=[])
    message = report.failure_message()
    assert "$412,000" in message
    assert "run the query" in message.lower()


def test_all_claims_are_reported_not_just_the_first() -> None:
    report = CHECKER.check("Spend 99.9, revenue 88.8, ROAS 7.7.", facts=[7.7])
    assert len(report.ungrounded) == 2


def test_an_answer_with_no_numbers_is_trivially_grounded() -> None:
    report = CHECKER.check("Branded search outperformed non-brand.", facts=[])
    assert report.ok
    assert report.checked == 0


def test_a_percentage_claim_is_not_grounded_by_an_unrelated_fraction() -> None:
    assert not CHECKER.check("CTR was 3.8%.", facts=[0.052]).ok


# ------------------------------------------------------- non-finite facts


def test_infinite_facts_do_not_crash_and_license_nothing() -> None:
    """An unguarded `avg(revenue/spend)` returns inf when spend is zero. It reaches here."""
    report = CHECKER.check("ROAS was 9.70.", facts=[float("inf"), float("-inf")])
    assert not report.ok


def test_nan_facts_do_not_crash() -> None:
    assert not CHECKER.check("ROAS was 9.70.", facts=[float("nan")]).ok


def test_a_finite_fact_still_grounds_alongside_non_finite_ones() -> None:
    assert CHECKER.check("ROAS was 9.70.", facts=[float("inf"), 9.7013]).ok
