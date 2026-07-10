"""Phase 2 tests: the tool layer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
from campaign_copilot.tools.python_exec import session_scope

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


def _in_session(session_id: str, sandbox: PythonSandbox, **kwargs: Any) -> ToolResult:
    """Run a cell as a given session. The scope is server state; the model cannot set it."""
    token = session_scope.set(session_id)
    try:
        return sandbox.run(**kwargs)
    finally:
        session_scope.reset(token)


def test_sandbox_captures_stdout(sandbox: PythonSandbox) -> None:
    result = _in_session("a", sandbox, code="print(6 * 7)")
    assert result.ok
    assert result.data["stdout"].strip() == "42"


def test_sandbox_state_persists_across_calls(sandbox: PythonSandbox) -> None:
    """A follow-up of "now plot that" only works if `that` is still in scope."""
    _in_session("b", sandbox, code="rows = [1, 2, 3]")
    result = _in_session("b", sandbox, code="print(sum(rows))")
    assert result.data["stdout"].strip() == "6"


def test_sessions_are_isolated_from_each_other(sandbox: PythonSandbox) -> None:
    _in_session("c", sandbox, code="secret = 1")
    result = _in_session("d", sandbox, code="print(secret)")
    assert not result.ok
    assert "NameError" in result.content


def test_reset_clears_a_session(sandbox: PythonSandbox) -> None:
    _in_session("e", sandbox, code="v = 1")
    sandbox.reset("e")
    assert not _in_session("e", sandbox, code="print(v)").ok


def test_a_runaway_loop_is_killed(sandbox: PythonSandbox) -> None:
    result = _in_session("f", sandbox, code="while True:\n    pass")
    assert not result.ok
    assert result.error_code == "TIMEOUT"


def test_a_memory_bomb_does_not_take_down_the_agent(sandbox: PythonSandbox) -> None:
    result = _in_session("g", sandbox, code="x = bytearray(10**10)")
    assert not result.ok
    assert result.error_code in {"EXECUTION_ERROR", "MEMORY_OR_CRASH"}


def test_network_access_is_denied(sandbox: PythonSandbox) -> None:
    """Best-effort, and documented as such. It stops the accidental call, not an attacker."""
    result = _in_session("h", sandbox, code="import socket; socket.socket()")
    assert not result.ok
    assert "PermissionError" in result.content


def test_subprocess_spawning_is_denied(sandbox: PythonSandbox) -> None:
    result = _in_session("i", sandbox, code="import subprocess; subprocess.run(['ls'])")
    assert not result.ok
    assert "PermissionError" in result.content


def test_a_traceback_is_returned_for_the_model_to_repair(sandbox: PythonSandbox) -> None:
    result = _in_session("j", sandbox, code="1 / 0")
    assert not result.ok
    assert result.error_code == "EXECUTION_ERROR"
    assert "ZeroDivisionError" in result.content


def test_unpicklable_names_are_dropped_and_reported(sandbox: PythonSandbox) -> None:
    """Losing a name silently would make the next turn fail for no visible reason."""
    result = _in_session("k", sandbox, code="keep = 1\ngen = (i for i in range(3))")
    assert result.ok
    assert "gen" in result.data["dropped"]
    assert "keep" in result.data["variables"]
    assert "dropped" in result.content


def test_empty_code_is_rejected(sandbox: PythonSandbox) -> None:
    assert _in_session("l", sandbox, code="   ").error_code == "EMPTY_CODE"


def test_the_model_cannot_choose_which_session_it_executes_in(sandbox: PythonSandbox) -> None:
    """The vulnerability from docs/AUDIT.md, P1-4.

    `session_id` used to be an argument in the tool's input schema, which put it in the
    model's action space: a persuaded agent could name another user's session and read their
    variables. It is now read from a ContextVar the server sets, and a model that passes it
    anyway is ignored.
    """
    assert "session_id" not in PythonSandbox.spec.input_schema["properties"]

    _in_session("alice", sandbox, code="password = 'hunter2'")

    # A model emitting `session_id="alice"` while the server says "mallory".
    token = session_scope.set("mallory")
    try:
        stolen = sandbox.run(code="print(password)", session_id="alice")
    finally:
        session_scope.reset(token)

    assert not stolen.ok
    assert "NameError" in stolen.content


def test_scratch_directories_do_not_accumulate(sandbox: PythonSandbox) -> None:
    """docs/AUDIT.md, R2-8. One directory per cell, in a process that runs for weeks."""
    for i in range(5):
        _in_session("gc", sandbox, code=f"x = {i}")
    assert not [p for p in sandbox.scratch.iterdir() if p.is_dir()]
