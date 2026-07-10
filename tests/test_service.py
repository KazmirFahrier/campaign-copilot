"""Phase 6 tests.

The api service and the executor service are exercised against each other over an in-process
ASGI transport: real HTTP semantics, real serialization, no network. The one test that matters
most is `test_the_executor_refuses_to_start_if_it_can_see_a_credential` -- it converts a
deployment property, which rots silently, into an assertion that fails loudly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from campaign_copilot.llm.client import ScriptedClient
from campaign_copilot.service.app import Settings, create_app
from campaign_copilot.service.executor import (
    SecretsVisibleError,
    assert_no_secrets,
)
from campaign_copilot.service.executor import (
    create_app as create_executor,
)
from campaign_copilot.tools.base import ToolResult, ToolSpec
from campaign_copilot.tools.python_exec import PythonSandbox, SandboxConfig
from campaign_copilot.tools.remote_exec import RemoteSandbox

DB = Path(__file__).resolve().parents[1] / "warehouse" / "campaign_copilot.duckdb"


def plan(action: str, **kw: Any) -> str:
    return json.dumps({"reasoning": "r", "action": action, **kw})


def tool_step(name: str, **arguments: Any) -> str:
    return plan("tool", tool_call={"tool": name, "arguments": arguments})


class StubTool:
    def __init__(self, name: str, result: ToolResult) -> None:
        self.spec = ToolSpec(
            name=name, description="stub", input_schema={"type": "object", "properties": {}}
        )
        self.result = result

    def run(self, **kwargs: Any) -> ToolResult:
        return self.result


def _app(responses: list[str], tools: dict[str, Any] | None = None) -> TestClient:
    registry = tools or {"t": StubTool("t", ToolResult.success("roas 9.7013", rows=[[9.7013]]))}
    app = create_app(
        settings=Settings(warehouse_path=DB),
        client_factory=lambda: ScriptedClient(responses),
        tools=registry,
    )
    return TestClient(app)


def _events(response: httpx.Response) -> list[dict[str, Any]]:
    return [
        json.loads(line[len("data: ") :])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


# --------------------------------------------------------------------- probes


def test_liveness_touches_nothing() -> None:
    """A dependency outage must not restart the process."""
    client = _app([])
    assert client.get("/healthz").json() == {"status": "ok"}


@pytest.mark.skipif(not DB.exists(), reason="run `make warehouse`")
def test_readiness_checks_the_warehouse() -> None:
    response = _app([]).get("/readyz")
    assert response.status_code == 200
    assert response.json()["checks"]["warehouse"] == "ok"


def test_readiness_fails_when_the_warehouse_is_missing() -> None:
    """Cloud Run must not route traffic to a revision whose dependencies are down."""
    app = create_app(
        settings=Settings(warehouse_path=Path("/nonexistent.duckdb")),
        client_factory=lambda: ScriptedClient([]),
        tools={},
    )
    response = TestClient(app).get("/readyz")
    assert response.status_code == 503
    assert not response.json()["ready"]


# ---------------------------------------------------------------- request ids


def test_a_request_id_is_generated_and_echoed() -> None:
    response = _app([]).get("/healthz")
    assert len(response.headers["X-Request-ID"]) == 16


def test_a_supplied_request_id_is_preserved() -> None:
    response = _app([]).get("/healthz", headers={"X-Request-ID": "abc123"})
    assert response.headers["X-Request-ID"] == "abc123"


# -------------------------------------------------------------------- streaming


def test_the_stream_carries_the_agents_decisions_in_order() -> None:
    client = _app([tool_step("t"), plan("answer", answer="ROAS was 9.70.")])
    response = client.post("/v1/chat", json={"question": "roas?"})
    assert response.status_code == 200

    kinds = [e["type"] for e in _events(response)]
    assert kinds[0] == "start"
    assert "tool_call" in kinds and "tool_result" in kinds
    assert "grounding" in kinds
    assert kinds[-1] == "done"


def test_the_final_event_carries_the_answer() -> None:
    client = _app([tool_step("t"), plan("answer", answer="ROAS was 9.70.")])
    events = _events(client.post("/v1/chat", json={"question": "roas?"}))
    assert events[-1] == {
        **events[-1],
        "type": "done",
        "ok": True,
        "answer": "ROAS was 9.70.",
    }


def test_every_event_carries_the_request_id() -> None:
    """So a user can quote one number in a bug report and we can find the whole trace."""
    client = _app([tool_step("t"), plan("answer", answer="ROAS was 9.70.")])
    response = client.post(
        "/v1/chat", json={"question": "roas?"}, headers={"X-Request-ID": "rid1"}
    )
    assert all(e["request_id"] == "rid1" for e in _events(response))


def test_the_stream_reports_a_blocked_answer_rather_than_shipping_it() -> None:
    client = _app(
        [
            tool_step("t"),
            plan("answer", answer="Spend was $412,000."),
            plan("answer", answer="Spend was $500,000."),
        ]
    )
    events = _events(client.post("/v1/chat", json={"question": "spend?"}))
    grounding = [e for e in events if e["type"] == "grounding"]
    assert grounding and not grounding[0]["ok"]
    assert "$412,000" in grounding[0]["ungrounded"]
    assert events[-1]["ok"] is False


def test_the_stream_always_terminates_even_when_the_agent_explodes() -> None:
    class Exploding:
        model = "boom"

        def complete(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("kaboom")

        def count_tokens(self, text: str) -> int:
            return 1

    app = create_app(
        settings=Settings(warehouse_path=DB),
        client_factory=Exploding,
        tools={},
    )
    events = _events(TestClient(app).post("/v1/chat", json={"question": "x"}))
    assert events[-1]["type"] == "error"


def test_an_empty_question_is_rejected_before_the_model_is_called() -> None:
    assert _app([]).post("/v1/chat", json={"question": ""}).status_code == 422


# ---------------------------------------------------------------------- metrics


def test_metrics_count_answers_and_blocked_answers_separately() -> None:
    client = _app([tool_step("t"), plan("answer", answer="ROAS was 9.70.")])
    client.post("/v1/chat", json={"question": "roas?"})
    snapshot = client.get("/metrics").json()
    assert snapshot["answers"] == 1
    assert snapshot["ungrounded_blocked"] == 0
    assert snapshot["p50_latency_ms"] >= 0


def test_a_blocked_answer_increments_the_counter_that_proves_the_gate_is_on() -> None:
    """`ungrounded_blocked` stuck at zero forever means somebody switched the gate off."""
    client = _app(
        [
            tool_step("t"),
            plan("answer", answer="Spend was $412,000."),
            plan("answer", answer="Spend was $500,000."),
        ]
    )
    client.post("/v1/chat", json={"question": "spend?"})
    snapshot = client.get("/metrics").json()
    assert snapshot["ungrounded_blocked"] == 1
    assert snapshot["answers"] == 0


# --------------------------------------------------------------------- executor


def test_the_executor_refuses_to_start_if_it_can_see_a_credential() -> None:
    """The whole architecture in one assertion.

    The executor's value is that arbitrary code execution inside it is uninteresting. One
    mounted secret destroys that, silently, and nothing else would notice. A misconfigured
    revision should crash-loop, not quietly become insecure.
    """
    with pytest.raises(SecretsVisibleError, match="ANTHROPIC_API_KEY"):
        assert_no_secrets({"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "sk-ant-real"})


@pytest.mark.parametrize(
    "name",
    [
        "OPENAI_API_KEY",
        "DB_PASSWORD",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "SLACK_TOKEN",
        "MY_SECRET",
    ],
)
def test_every_shape_of_credential_is_caught(name: str) -> None:
    with pytest.raises(SecretsVisibleError):
        assert_no_secrets({name: "x"})


def test_the_platforms_own_variables_are_not_credentials() -> None:
    assert_no_secrets(
        {"PATH": "/usr/bin", "K_SERVICE": "executor", "PORT": "8080", "HOME": "/x"}
    )


def test_the_executor_runs_code_and_persists_session_state() -> None:
    executor = create_executor(
        PythonSandbox(config=SandboxConfig(timeout_seconds=5, cpu_seconds=5))
    )
    client = TestClient(executor)

    first = client.post("/exec", json={"code": "rows = [1, 2, 3]", "session_id": "s"})
    assert first.json()["ok"]
    second = client.post("/exec", json={"code": "print(sum(rows))", "session_id": "s"})
    assert second.json()["data"]["stdout"].strip() == "6"


def test_the_remote_sandbox_is_indistinguishable_from_the_local_one() -> None:
    """Same spec, same ToolResult contract. The agent cannot tell where the boundary is."""
    assert RemoteSandbox.spec is PythonSandbox.spec

    executor = create_executor(
        PythonSandbox(config=SandboxConfig(timeout_seconds=5, cpu_seconds=5))
    )
    # TestClient *is* an httpx.Client with a sync ASGI transport, which is exactly what a
    # RemoteSandbox wants. httpx.ASGITransport is async-only and cannot serve one.
    remote = RemoteSandbox(base_url="http://executor", client=TestClient(executor))

    ok = remote.run(code="print(6 * 7)", session_id="a")
    assert ok.ok and ok.data["stdout"].strip() == "42"

    bad = remote.run(code="1 / 0", session_id="a")
    assert not bad.ok
    assert bad.error_code == "EXECUTION_ERROR"
    assert "ZeroDivisionError" in bad.content
    assert not bad.content.startswith("EXECUTION_ERROR: EXECUTION_ERROR")


def test_an_unreachable_executor_is_infrastructure_not_a_bug_in_the_model_s_code() -> None:
    """Telling the agent its Python was wrong, when the network was, makes it repair nothing."""
    remote = RemoteSandbox(
        base_url="http://executor",
        client=httpx.Client(
            transport=httpx.MockTransport(lambda _r: httpx.Response(503)),
            base_url="http://executor",
        ),
    )
    result = remote.run(code="print(1)")
    assert result.error_code == "EXECUTOR_UNAVAILABLE"


def test_an_executor_timeout_tells_the_agent_not_to_retry() -> None:
    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow")

    remote = RemoteSandbox(
        base_url="http://executor",
        client=httpx.Client(transport=httpx.MockTransport(boom), base_url="http://executor"),
    )
    result = remote.run(code="print(1)")
    assert result.error_code == "EXECUTOR_TIMEOUT"
    assert "do not retry" in result.content.lower()
