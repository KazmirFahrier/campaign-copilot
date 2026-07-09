"""The guardrail is the security boundary. Test it like one."""

from __future__ import annotations

import pytest

from campaign_copilot.guardrails.sql_guard import (
    GuardrailViolation,
    SqlGuard,
    SqlGuardConfig,
    ViolationCode,
)
from campaign_copilot.semantic.layer import SemanticLayer

TABLE = "main_marts.campaign_performance_daily"


@pytest.fixture(scope="module")
def layer() -> SemanticLayer:
    return SemanticLayer.load()


@pytest.fixture(scope="module")
def guard(layer: SemanticLayer) -> SqlGuard:
    return SqlGuard(SqlGuardConfig(allowed_aggregates=layer.aggregate_atoms()))


# ----------------------------------------------------------------- happy path


def test_registered_aggregate_passes(guard: SqlGuard) -> None:
    result = guard.check(
        f"select channel, sum(revenue_usd) / nullif(sum(spend_usd), 0) as roas "
        f"from {TABLE} group by 1"
    )
    assert "roas" in result.sql.lower()


def test_cte_is_allowed_and_not_mistaken_for_a_table(guard: SqlGuard) -> None:
    sql = f"""
        with base as (select channel, sum(spend_usd) as spend_usd from {TABLE} group by 1)
        select channel, spend_usd from base
    """
    assert guard.check(sql).sql


def test_limit_is_injected_when_absent(guard: SqlGuard) -> None:
    result = guard.check(f"select channel, sum(clicks) from {TABLE} group by 1")
    assert "LIMIT 100" in result.sql.upper()


def test_excessive_limit_is_clamped_with_a_warning(guard: SqlGuard) -> None:
    result = guard.check(f"select channel, sum(clicks) from {TABLE} group by 1 limit 999999")
    assert "LIMIT 10000" in result.sql.upper()
    assert result.warnings and "clamped" in result.warnings[0]


def test_count_star_is_permitted(guard: SqlGuard) -> None:
    assert guard.check(f"select count(*) from {TABLE}").sql


# ------------------------------------------------------------------ rejection


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        (f"drop table {TABLE}", ViolationCode.NOT_A_SELECT),
        (f"delete from {TABLE}", ViolationCode.NOT_A_SELECT),
        (f"insert into {TABLE} values (1)", ViolationCode.NOT_A_SELECT),
        (f"update {TABLE} set spend_usd = 0", ViolationCode.NOT_A_SELECT),
        ("attach 'evil.db' as evil", ViolationCode.NOT_A_SELECT),
        ("install httpfs", ViolationCode.NOT_A_SELECT),
        (f"copy {TABLE} to '/tmp/x.csv'", ViolationCode.NOT_A_SELECT),
    ],
)
def test_non_select_statements_are_rejected(guard: SqlGuard, sql: str, code: str) -> None:
    with pytest.raises(GuardrailViolation) as err:
        guard.check(sql)
    assert err.value.code == code


def test_statement_batching_is_rejected(guard: SqlGuard) -> None:
    with pytest.raises(GuardrailViolation) as err:
        guard.check(f"select channel from {TABLE}; drop table {TABLE};")
    assert err.value.code == ViolationCode.MULTIPLE_STATEMENTS


def test_comment_obfuscated_ddl_does_not_slip_through(guard: SqlGuard) -> None:
    """String matching on 'DROP' fails here. AST parsing does not."""
    with pytest.raises(GuardrailViolation):
        guard.check(f"select channel from {TABLE}; /**/ dRoP /**/ table {TABLE}")


def test_filesystem_function_is_rejected(guard: SqlGuard) -> None:
    with pytest.raises(GuardrailViolation) as err:
        guard.check("select col from read_csv('/etc/passwd')")
    assert err.value.code in {
        ViolationCode.FUNCTION_NOT_ALLOWED,
        ViolationCode.TABLE_NOT_ALLOWED,
    }


def test_unknown_table_is_rejected(guard: SqlGuard) -> None:
    with pytest.raises(GuardrailViolation) as err:
        guard.check("select secret from main.api_keys")
    assert err.value.code == ViolationCode.TABLE_NOT_ALLOWED


def test_select_star_is_rejected(guard: SqlGuard) -> None:
    with pytest.raises(GuardrailViolation) as err:
        guard.check(f"select * from {TABLE}")
    assert err.value.code == ViolationCode.STAR_NOT_ALLOWED


def test_average_of_a_ratio_is_rejected(guard: SqlGuard) -> None:
    """The headline rule. An LLM writes this constantly; it is arithmetically wrong."""
    with pytest.raises(GuardrailViolation) as err:
        guard.check(
            f"select channel, avg(revenue_usd / spend_usd) as roas from {TABLE} group by 1"
        )
    assert err.value.code == ViolationCode.UNREGISTERED_AGGREGATE


def test_unregistered_aggregate_over_a_fact_column_is_rejected(guard: SqlGuard) -> None:
    with pytest.raises(GuardrailViolation) as err:
        guard.check(f"select channel, median(spend_usd) from {TABLE} group by 1")
    assert err.value.code == ViolationCode.UNREGISTERED_AGGREGATE
