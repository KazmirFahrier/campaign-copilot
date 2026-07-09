"""Phase 2 tests: the tool layer."""

from __future__ import annotations

from pathlib import Path

import pytest

from campaign_copilot.guardrails.sql_guard import SqlGuard, SqlGuardConfig
from campaign_copilot.semantic.layer import SemanticLayer
from campaign_copilot.tools import (
    ListMetricsTool,
    PythonSandbox,
    QueryMetricsTool,
    RunSqlTool,
    SandboxConfig,
    ToolResult,
    Warehouse,
)

DB = Path(__file__).resolve().parents[1] / "warehouse" / "campaign_copilot.duckdb"
TABLE = "main_marts.campaign_performance_daily"


@pytest.fixture(scope="module")
def layer() -> SemanticLayer:
    return SemanticLayer.load()


@pytest.fixture(scope="module")
def warehouse() -> Warehouse:
    if not DB.exists():
        pytest.skip("warehouse not built; run `make warehouse`")
    return Warehouse(DB)


@pytest.fixture(scope="module")
def query_tool(layer: SemanticLayer, warehouse: Warehouse) -> QueryMetricsTool:
    return QueryMetricsTool(layer=layer, warehouse=warehouse)


@pytest.fixture(scope="module")
def sql_tool(layer: SemanticLayer, warehouse: Warehouse) -> RunSqlTool:
    guard = SqlGuard(SqlGuardConfig(allowed_aggregates=layer.aggregate_atoms()))
    return RunSqlTool(guard=guard, warehouse=warehouse)


# ------------------------------------------------------------------- contract


def test_a_failure_is_a_result_not_an_exception(sql_tool: RunSqlTool) -> None:
    """The model must see the error code, not a stack trace."""
    result = sql_tool.run(sql=f"drop table {TABLE}")
    assert isinstance(result, ToolResult)
    assert not result.ok
    assert result.error_code == "NOT_A_SELECT"


def test_numeric_facts_walks_nested_payloads() -> None:
    result = ToolResult.success("x", rows=[[1.5, None], [2.5, "text"]], row_count=2)
    assert sorted(result.numeric_facts()) == [1.5, 2.0, 2.5]


def test_booleans_are_not_numeric_facts() -> None:
    assert ToolResult.success("x", flag=True).numeric_facts() == []


# ---------------------------------------------------------------- metadata tool


def test_list_metrics_surfaces_ambiguity(layer: SemanticLayer) -> None:
    result = ListMetricsTool(layer=layer).run()
    assert result.ok
    assert "AMBIGUITY" in result.content
    assert "blended_roas" in result.content


# ------------------------------------------------------------------ safe path


def test_query_metrics_compiles_and_executes(query_tool: QueryMetricsTool) -> None:
    result = query_tool.run(metrics=["spend", "roas"], dimensions=["channel"], order_by="spend")
    assert result.ok
    assert result.data["row_count"] > 0
    assert "sum(revenue_usd)" in result.data["sql"]


def test_query_metrics_attaches_ambiguity_notes(query_tool: QueryMetricsTool) -> None:
    """Asking for ROAS without saying blended-or-not must force the agent to choose."""
    result = query_tool.run(metrics=["roas"], dimensions=["channel"])
    assert "AMBIGUITY NOTES" in result.content


def test_unambiguous_metric_gets_no_notes(query_tool: QueryMetricsTool) -> None:
    result = query_tool.run(metrics=["ctr"], dimensions=["channel"])
    assert "AMBIGUITY NOTES" not in result.content


def test_filtering_a_channel_warns_about_branded_search(query_tool: QueryMetricsTool) -> None:
    result = query_tool.run(metrics=["ctr"], filters=["channel like 'paid_search%'"])
    assert "AMBIGUITY NOTES" in result.content
    assert "brand" in result.content.lower()


def test_unknown_metric_returns_a_repairable_error(query_tool: QueryMetricsTool) -> None:
    result = query_tool.run(metrics=["roass"])
    assert not result.ok
    assert result.error_code == "SEMANTIC_ERROR"
    assert "Did you mean" in result.content


def test_metric_below_its_grain_is_refused(query_tool: QueryMetricsTool) -> None:
    result = query_tool.run(metrics=["blended_roas"], dimensions=["campaign_name"])
    assert not result.ok
    assert "not defined at grain" in result.content


# --------------------------------------------------------------- escape hatch


def test_run_sql_executes_a_guarded_query(sql_tool: RunSqlTool) -> None:
    result = sql_tool.run(
        sql=f"select channel, sum(clicks) as clicks from {TABLE} group by 1 limit 3"
    )
    assert result.ok
    assert result.data["row_count"] == 3


def test_run_sql_rejects_invented_arithmetic(sql_tool: RunSqlTool) -> None:
    result = sql_tool.run(
        sql=f"select channel, avg(revenue_usd / spend_usd) from {TABLE} group by 1"
    )
    assert not result.ok
    assert result.error_code == "UNREGISTERED_AGGREGATE"


def test_run_sql_reports_the_clamped_limit(sql_tool: RunSqlTool) -> None:
    result = sql_tool.run(sql=f"select channel from {TABLE} limit 999999")
    assert result.ok
    assert "clamped" in result.content


# ---------------------------------------------------------------------- sandbox


@pytest.fixture
def sandbox() -> PythonSandbox:
    return PythonSandbox(config=SandboxConfig(timeout_seconds=5, cpu_seconds=5, memory_mb=256))


def test_sandbox_captures_stdout(sandbox: PythonSandbox) -> None:
    result = sandbox.run(code="print(6 * 7)", session_id="a")
    assert result.ok
    assert result.data["stdout"].strip() == "42"


def test_sandbox_state_persists_across_calls(sandbox: PythonSandbox) -> None:
    """A follow-up of "now plot that" only works if `that` is still in scope."""
    sandbox.run(code="rows = [1, 2, 3]", session_id="b")
    result = sandbox.run(code="print(sum(rows))", session_id="b")
    assert result.data["stdout"].strip() == "6"


def test_sessions_are_isolated_from_each_other(sandbox: PythonSandbox) -> None:
    sandbox.run(code="secret = 1", session_id="c")
    result = sandbox.run(code="print(secret)", session_id="d")
    assert not result.ok
    assert "NameError" in result.content


def test_reset_clears_a_session(sandbox: PythonSandbox) -> None:
    sandbox.run(code="v = 1", session_id="e")
    sandbox.reset("e")
    assert not sandbox.run(code="print(v)", session_id="e").ok


def test_a_runaway_loop_is_killed(sandbox: PythonSandbox) -> None:
    result = sandbox.run(code="while True:\n    pass", session_id="f")
    assert not result.ok
    assert result.error_code == "TIMEOUT"


def test_a_memory_bomb_does_not_take_down_the_agent(sandbox: PythonSandbox) -> None:
    result = sandbox.run(code="x = bytearray(10**10)", session_id="g")
    assert not result.ok
    assert result.error_code in {"EXECUTION_ERROR", "MEMORY_OR_CRASH"}


def test_network_access_is_denied(sandbox: PythonSandbox) -> None:
    """Best-effort, and documented as such. It stops the accidental call, not an attacker."""
    result = sandbox.run(code="import socket; socket.socket()", session_id="h")
    assert not result.ok
    assert "PermissionError" in result.content


def test_subprocess_spawning_is_denied(sandbox: PythonSandbox) -> None:
    result = sandbox.run(code="import subprocess; subprocess.run(['ls'])", session_id="i")
    assert not result.ok
    assert "PermissionError" in result.content


def test_a_traceback_is_returned_for_the_model_to_repair(sandbox: PythonSandbox) -> None:
    result = sandbox.run(code="1 / 0", session_id="j")
    assert not result.ok
    assert result.error_code == "EXECUTION_ERROR"
    assert "ZeroDivisionError" in result.content


def test_unpicklable_names_are_dropped_and_reported(sandbox: PythonSandbox) -> None:
    """Losing a name silently would make the next turn fail for no visible reason."""
    result = sandbox.run(code="keep = 1\ngen = (i for i in range(3))", session_id="k")
    assert result.ok
    assert "gen" in result.data["dropped"]
    assert "keep" in result.data["variables"]
    assert "dropped" in result.content


def test_empty_code_is_rejected(sandbox: PythonSandbox) -> None:
    assert sandbox.run(code="   ", session_id="l").error_code == "EMPTY_CODE"
