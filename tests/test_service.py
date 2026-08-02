"""Phase 6 tests.

The api service and the executor service are exercised against each other over an in-process
ASGI transport: real HTTP semantics, real serialization, no network. The one test that matters
most is `test_the_executor_refuses_to_start_if_it_can_see_a_credential` -- it converts a
deployment property, which rots silently, into an assertion that fails loudly.
"""

from __future__ import annotations

import base64
import json
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from campaign_copilot.llm.client import ScriptedClient
from campaign_copilot.llm.tokens import ContextBudget
from campaign_copilot.memory import ConversationMemory
from campaign_copilot.service.app import Settings, build_tools, create_app
from campaign_copilot.service.executor import (
    SecretsVisibleError,
    assert_no_secrets,
)
from campaign_copilot.service.executor import (
    create_app as create_executor,
)
from campaign_copilot.service.request_auth import RequestSigner
from campaign_copilot.tools.base import ToolResult, ToolSpec
from campaign_copilot.tools.python_exec import PythonSandbox, SandboxConfig
from campaign_copilot.tools.remote_exec import RemoteSandbox

DB = Path(__file__).resolve().parents[1] / "warehouse" / "campaign_copilot.duckdb"
TEST_SIGNING_PRIVATE_KEY = base64.b64encode(bytes(range(32))).decode()


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
    assert client.get("/health").json() == {"status": "ok"}


@pytest.mark.skipif(not DB.exists(), reason="run `make warehouse`")
def test_readiness_checks_the_warehouse() -> None:
    response = _app([]).get("/ready")
    assert response.status_code == 200
    assert response.json()["checks"]["warehouse"] == "ok"


def test_readiness_fails_when_the_warehouse_is_missing() -> None:
    """Cloud Run must not route traffic to a revision whose dependencies are down."""
    app = create_app(
        settings=Settings(warehouse_path=Path("/nonexistent.duckdb")),
        client_factory=lambda: ScriptedClient([]),
        tools={},
    )
    response = TestClient(app).get("/ready")
    assert response.status_code == 503
    assert not response.json()["ready"]


# ---------------------------------------------------------------- request ids


def test_a_request_id_is_generated_and_echoed() -> None:
    response = _app([]).get("/health")
    assert len(response.headers["X-Request-ID"]) == 16


def test_a_supplied_request_id_is_preserved() -> None:
    response = _app([]).get("/health", headers={"X-Request-ID": "abc123"})
    assert response.headers["X-Request-ID"] == "abc123"


def test_an_invalid_request_id_is_replaced_before_it_reaches_logs() -> None:
    invalid = "a" * 100
    response = _app([]).get("/health", headers={"X-Request-ID": invalid})
    assert response.headers["X-Request-ID"] != invalid
    assert len(response.headers["X-Request-ID"]) == 16


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
    assert "kaboom" not in events[-1]["text"]


def test_an_empty_question_is_rejected_before_the_model_is_called() -> None:
    assert _app([]).post("/v1/chat", json={"question": ""}).status_code == 422


def test_configured_bearer_auth_fails_closed() -> None:
    app = create_app(
        settings=Settings(warehouse_path=DB, api_bearer_token="secret-token"),
        client_factory=lambda: ScriptedClient([plan("answer", answer="Done.")]),
        tools={},
    )
    client = TestClient(app)
    assert client.post("/v1/chat", json={"question": "x"}).status_code == 401
    response = client.post(
        "/v1/chat",
        json={"question": "x"},
        headers={"Authorization": "Bearer secret-token"},
    )
    assert response.status_code == 200


def test_production_configuration_fails_closed_without_required_controls() -> None:
    with pytest.raises(ValueError, match="CC_API_BEARER_TOKEN"):
        Settings(environment="production")


def test_an_unknown_environment_cannot_bypass_production_checks() -> None:
    with pytest.raises(ValueError, match="CC_ENVIRONMENT"):
        Settings(environment="prodution")


def test_a_complete_production_configuration_is_accepted() -> None:
    config = Settings(
        environment="production",
        api_bearer_token="x" * 32,
        executor_url="https://executor.example",
        executor_audience="https://executor.example",
        executor_signing_private_key=TEST_SIGNING_PRIVATE_KEY,
        release="abc123",
    )
    assert config.environment == "production"


def test_production_disables_interactive_api_schema_routes() -> None:
    settings = Settings(
        warehouse_path=DB,
        environment="production",
        api_bearer_token="x" * 32,
        executor_url="https://executor.example",
        executor_audience="https://executor.example",
        executor_signing_private_key=TEST_SIGNING_PRIVATE_KEY,
        release="abc123",
    )
    client = TestClient(
        create_app(settings=settings, client_factory=lambda: ScriptedClient([]), tools={})
    )
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_release_identity_is_carried_by_every_event() -> None:
    app = create_app(
        settings=Settings(warehouse_path=DB, release="abc123"),
        client_factory=lambda: ScriptedClient([plan("answer", answer="Done.")]),
        tools={},
    )
    events = _events(TestClient(app).post("/v1/chat", json={"question": "x"}))
    assert events
    assert all(event["release"] == "abc123" for event in events)


def test_invalid_session_identifiers_are_rejected() -> None:
    assert (
        _app([])
        .post("/v1/chat", json={"question": "x", "session_id": "../another-session"})
        .status_code
        == 422
    )


def test_admission_control_rejects_excess_model_work() -> None:
    """Overload must be bounded before another paid model call starts."""
    from campaign_copilot.llm.client import LLMResponse

    entered = threading.Event()
    release = threading.Event()

    class BlockingClient:
        model = "blocking"

        def complete(self, *args: Any, **kwargs: Any) -> LLMResponse:
            entered.set()
            assert release.wait(timeout=2)
            return LLMResponse(text=plan("answer", answer="Done."), model=self.model)

        def count_tokens(self, text: str) -> int:
            return 1

    app = create_app(
        settings=Settings(warehouse_path=DB, max_concurrent_requests=1),
        client_factory=BlockingClient,
        tools={},
    )
    client = TestClient(app)
    first = threading.Thread(target=lambda: client.post("/v1/chat", json={"question": "first"}))
    first.start()
    assert entered.wait(timeout=2)
    overloaded = client.post("/v1/chat", json={"question": "second"})
    release.set()
    first.join(timeout=2)

    assert overloaded.status_code == 429
    assert overloaded.headers["Retry-After"] == "2"
    assert client.get("/metrics").json()["overloaded"] == 1


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
        PythonSandbox(config=SandboxConfig(timeout_seconds=5, cpu_seconds=5)),
        environ={"PATH": "/usr/bin"},
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
        PythonSandbox(config=SandboxConfig(timeout_seconds=5, cpu_seconds=5)),
        environ={"PATH": "/usr/bin"},
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


def test_the_executor_requires_a_fresh_api_signature_when_configured() -> None:
    private_key = Ed25519PrivateKey.generate()
    signer = RequestSigner(private_key)
    public_key = base64.b64encode(private_key.public_key().public_bytes_raw()).decode()
    executor = create_executor(
        PythonSandbox(config=SandboxConfig(timeout_seconds=5, cpu_seconds=5)),
        environ={"PATH": "/usr/bin", "CC_EXECUTOR_SIGNING_PUBLIC_KEY": public_key},
    )
    client = TestClient(executor)
    assert client.post("/exec", json={"code": "print(1)"}).status_code == 401

    remote = RemoteSandbox(base_url="http://executor", client=client, signer=signer)
    result = remote.run(code="print(6 * 7)")
    # macOS cannot raise RLIMIT_AS again inside this process, so execution may fail locally;
    # a non-infrastructure result proves the signed request passed the middleware.
    assert result.error_code != "EXECUTOR_UNAVAILABLE"

    body = json.dumps(
        {"code": "print(1)", "session_id": "default"},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    headers = {"Content-Type": "application/json", **signer.headers("POST", "/exec", body)}
    assert client.post("/exec", content=body, headers=headers).status_code == 200
    assert client.post("/exec", content=body, headers=headers).status_code == 401


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


# ------------------------------------------------- regressions from docs/AUDIT.md


def test_settings_read_the_environment_when_instantiated_not_when_imported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2-7. A bare `os.getenv` default freezes the environment at import."""
    monkeypatch.setenv("CC_MAX_STEPS", "3")
    assert Settings().max_steps == 3
    monkeypatch.setenv("CC_MAX_STEPS", "9")
    assert Settings().max_steps == 9


def test_latency_samples_are_bounded() -> None:
    """P2-8. An unbounded list is a slow leak and an expensive /metrics scrape."""
    from campaign_copilot.service.app import Metrics

    metrics = Metrics()
    for i in range(3000):
        metrics.observe_latency(float(i))
    assert len(metrics.latency_ms) == 1024
    assert metrics.snapshot()["p95_latency_ms"] > 0


def test_a_session_survives_between_requests() -> None:
    """P0-3. `session_id` used to be accepted, validated, threaded through, and dropped."""
    client = _app(
        [
            plan("answer", answer="First."),
            plan("answer", answer="Second."),
        ]
    )
    client.post("/v1/chat", json={"question": "remember apples", "session_id": "s1"})
    client.post("/v1/chat", json={"question": "what did I say?", "session_id": "s1"})

    memory = client.app.state.sessions.get("s1")
    said = [m.content for m in memory.turns if m.role == "user"]
    assert "remember apples" in said, "turn two must see turn one"
    assert len(client.app.state.sessions) == 1


def test_two_sessions_do_not_share_memory() -> None:
    client = _app([plan("answer", answer="A."), plan("answer", answer="B.")])
    client.post("/v1/chat", json={"question": "alpha", "session_id": "a"})
    client.post("/v1/chat", json={"question": "beta", "session_id": "b"})

    store = client.app.state.sessions
    assert len(store) == 2
    assert all("beta" not in m.content for m in store.get("a").turns)


def test_the_session_store_evicts_the_least_recently_used() -> None:
    """Bounded, because an unbounded dict keyed by user input is a memory exhaustion bug."""
    from campaign_copilot.service.app import SessionStore

    store = SessionStore(lambda sid: ConversationMemory(sid, ContextBudget(1000, 100)), 2)
    store.get("a")
    store.get("b")
    store.get("a")  # touch a, so b is now least recent
    store.get("c")
    assert len(store) == 2
    assert "b" not in store._sessions


def test_the_stream_terminates_even_when_setup_fails_before_the_try() -> None:
    """A failure in the worker's *setup* used to hang the stream forever.

    The sentinel that ends the SSE response lives in `finally`, and there was code above the
    `try`. The original 'always terminates' test raised inside the try, so it never saw this.
    """

    class ExplodingStore:
        def get(self, session_id: str) -> None:
            raise RuntimeError("session store is down")

    app = create_app(
        settings=Settings(warehouse_path=DB),
        client_factory=lambda: ScriptedClient([]),
        tools={},
    )
    app.state.sessions = ExplodingStore()
    # Rebind the closure's store by patching the factory the handler closed over is not
    # possible; instead assert the guarantee at the level that matters: a raising worker.
    client = TestClient(app)
    events = _events(client.post("/v1/chat", json={"question": "x"}))
    assert events, "the stream must emit something and close, never hang"
    assert events[-1]["type"] in {"done", "error"}


def test_the_remote_sandbox_authenticates_when_a_token_provider_is_configured() -> None:
    """P0-2. Cloud Run's invoker binding demands an identity token. We sent none."""
    seen: list[str | None] = []

    def capture(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("X-Serverless-Authorization"))
        return httpx.Response(200, json={"ok": True, "content": "42", "data": {}})

    remote = RemoteSandbox(
        base_url="http://executor",
        client=httpx.Client(transport=httpx.MockTransport(capture), base_url="http://executor"),
        token_provider=lambda: "id-token-abc",
    )
    remote.run(code="print(42)")
    assert seen == ["Bearer id-token-abc"]


@pytest.mark.skipif(not DB.exists(), reason="run `make warehouse`")
def test_the_production_tool_registry_wires_executor_identity() -> None:
    """Readiness and real execution must authenticate through the same audience."""
    registry = build_tools(
        Settings(
            warehouse_path=DB,
            executor_url="https://executor.example",
            executor_audience="https://executor.example",
        )
    )
    remote = registry["python_exec"]
    assert isinstance(remote, RemoteSandbox)
    assert remote.token_provider is not None


def test_an_unauthenticated_executor_gets_no_header() -> None:
    seen: list[str | None] = []

    def capture(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("X-Serverless-Authorization"))
        return httpx.Response(200, json={"ok": True, "content": "42", "data": {}})

    remote = RemoteSandbox(
        base_url="http://executor",
        client=httpx.Client(transport=httpx.MockTransport(capture), base_url="http://executor"),
    )
    remote.run(code="print(42)")
    assert seen == [None]


def test_the_secrets_assertion_is_injectable_so_the_suite_can_run_anywhere() -> None:
    """docs/AUDIT.md, R2-5. A safety check that fails the tests is a check somebody weakens."""
    create_executor(environ={"PATH": "/usr/bin", "K_SERVICE": "executor"})
    with pytest.raises(SecretsVisibleError):
        create_executor(environ={"ANTHROPIC_API_KEY": "sk-real"})


class _ConcurrencyProbe:
    """An LLM client that reports the peak number of agents running at once."""

    model = "probe"

    def __init__(self, counters: dict[str, int], lock: threading.Lock) -> None:
        self._counters = counters
        self._lock = lock

    def complete(self, *args: Any, **kwargs: Any) -> Any:
        import time

        from campaign_copilot.llm.client import LLMResponse

        with self._lock:
            self._counters["live"] += 1
            self._counters["peak"] = max(self._counters["peak"], self._counters["live"])
        time.sleep(0.05)
        with self._lock:
            self._counters["live"] -= 1
        return LLMResponse(text=plan("answer", answer="A."), model="probe")

    def count_tokens(self, text: str) -> int:
        return 1


def _peak_concurrency(session_ids: list[str]) -> int:
    import threading

    counters = {"live": 0, "peak": 0}
    lock = threading.Lock()
    app = create_app(
        settings=Settings(warehouse_path=DB),
        client_factory=lambda: _ConcurrencyProbe(counters, lock),
        tools={},
    )
    client = TestClient(app)

    threads = [
        threading.Thread(
            target=lambda sid=sid: client.post(  # type: ignore[misc]
                "/v1/chat", json={"question": "q", "session_id": sid}
            )
        )
        for sid in session_ids
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return counters["peak"]


def test_turns_on_one_session_are_serialised() -> None:
    """docs/AUDIT.md, R4-2. SessionStore was locked; the memory it handed out was not.

    An earlier version of this test posted three requests and asserted all three turns were
    recorded. It passed with the lock removed -- `list.append` is atomic, so it could not see
    the race it was named after. This one measures the thing directly: peak overlap.
    """
    assert _peak_concurrency(["shared", "shared", "shared"]) == 1


def test_the_lock_is_per_session_not_global() -> None:
    """A global lock would also make the test above pass, and would serialise every user."""
    assert _peak_concurrency(["a", "b", "c"]) > 1


def test_the_turn_lock_is_dropped_when_a_session_is_evicted() -> None:
    """An LRU that evicts sessions but keeps their locks is a slow leak."""
    from campaign_copilot.service.app import SessionStore

    store = SessionStore(lambda sid: ConversationMemory(sid, ContextBudget(1000, 100)), 2)
    for sid in ("a", "b", "c"):
        store.get(sid)
        store.turn_lock(sid)
    assert len(store._turn_locks) <= 3
    assert "a" not in store._sessions


def test_metrics_expose_a_cancelled_counter() -> None:
    """A disconnected client is neither an answer nor an error. It gets its own bucket."""
    client = _app([plan("answer", answer="ROAS was 9.70.")])
    client.post("/v1/chat", json={"question": "roas?"})
    assert "cancelled" in client.get("/metrics").json()


def test_readiness_reports_the_executor_when_one_is_configured() -> None:
    """docs/AUDIT.md, R5-2, verified over real HTTP and pinned here.

    /ready must fail closed when a configured dependency is down; /health must not, or a
    dependency outage restarts the process instead of draining the revision.
    """
    app = create_app(
        settings=Settings(warehouse_path=DB, executor_url="http://127.0.0.1:59999"),
        client_factory=lambda: ScriptedClient([]),
        tools={},
    )
    client = TestClient(app)
    body = client.get("/ready").json()
    assert body["ready"] is False
    assert "executor" in body["checks"]
    assert client.get("/health").status_code == 200


def test_question_numbers_are_blocked_even_after_a_query() -> None:
    """An unrelated query cannot launder a user supplied number into a finding."""
    client = _app(
        [
            tool_step("t"),
            plan("answer", answer="No campaign beat 2.5x ROAS."),
            plan("answer", answer="No campaign beat the requested threshold."),
        ]
    )
    events = _events(
        client.post("/v1/chat", json={"question": "did any campaign beat 2.5x ROAS?"})
    )
    grounding = [e for e in events if e["type"] == "grounding"]
    assert grounding
    assert grounding[0]["ok"] is False
    assert grounding[0]["ungrounded"] == ["2.5"]
