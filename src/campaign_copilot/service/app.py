"""The API service.

Streams the agent's *decisions*, not its tokens. Our `LLMClient` protocol does not stream, and
faking a token stream by chunking a finished string is theatre. What a person waiting fifteen
seconds on a warehouse query actually wants to see is the plan, the SQL, the tool verdict, and
the grounding result -- which is exactly what `AgentTrace` already records. So the SSE stream
carries those events, and the last one carries the answer.

Operational shape:

* `/healthz` is liveness: the process is up. It touches nothing.
* `/readyz` is readiness: the warehouse answers and the executor answers. Cloud Run must not
  route traffic to a revision whose dependencies are down, and conflating the two probes is
  how a bad revision takes an outage with it.
* Every request carries an `X-Request-ID`, generated if absent, echoed on the response, bound
  into every log line for that request, and included in the SSE stream so a user can quote it
  in a bug report.
* Logs are single-line JSON. A log format that needs a regex to parse is a log format that
  gets parsed wrong at 3am.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from campaign_copilot.agent.loop import Agent
from campaign_copilot.grounding import GroundingChecker
from campaign_copilot.guardrails.sql_guard import SqlGuard, SqlGuardConfig
from campaign_copilot.llm.client import AnthropicClient, GeminiClient, LLMClient, OpenAIClient
from campaign_copilot.llm.tokens import ContextBudget
from campaign_copilot.memory import ConversationMemory
from campaign_copilot.rag import HybridRetriever, build_corpus
from campaign_copilot.semantic.layer import SemanticLayer
from campaign_copilot.tools.python_exec import PythonSandbox, SandboxConfig, session_scope
from campaign_copilot.tools.remember import RememberTool
from campaign_copilot.tools.retrieve import SearchDocsTool
from campaign_copilot.tools.sql import ListMetricsTool, QueryMetricsTool, RunSqlTool, Warehouse

__all__ = ["Metrics", "Settings", "create_app", "request_id"]

request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class _JsonFormatter(logging.Formatter):
    """One line of JSON per event, with the request id already attached."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.time(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id.get(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        payload.update(getattr(record, "extra_fields", {}))
        return json.dumps(payload)


def configure_logging(level: int = logging.INFO) -> None:
    """Replace the root handler with structured JSON."""
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the service reads from its environment, in one place.

    Every default is a `default_factory`. A bare ``os.getenv(...)`` default is evaluated once,
    when the class body runs, so every ``Settings()`` returns the environment as it was at
    import time -- which is invisible in Cloud Run and maddening everywhere else
    (docs/AUDIT.md, P2-7).
    """

    warehouse_path: Path = field(
        default_factory=lambda: Path(
            os.getenv("CC_WAREHOUSE", "warehouse/campaign_copilot.duckdb")
        )
    )
    executor_url: str | None = field(
        default_factory=lambda: os.getenv("CC_EXECUTOR_URL") or None
    )
    executor_audience: str | None = field(
        default_factory=lambda: os.getenv("CC_EXECUTOR_AUDIENCE") or None
    )
    llm_provider: str = field(default_factory=lambda: os.getenv("CC_LLM_PROVIDER", "anthropic"))
    model: str = field(default_factory=lambda: os.getenv("CC_MODEL", "claude-sonnet-4-5"))
    google_cloud_project: str | None = field(
        default_factory=lambda: os.getenv("GOOGLE_CLOUD_PROJECT") or None
    )
    google_cloud_location: str = field(
        default_factory=lambda: os.getenv("GOOGLE_CLOUD_LOCATION", "global")
    )
    max_steps: int = field(default_factory=lambda: int(os.getenv("CC_MAX_STEPS", "8")))
    context_window: int = field(
        default_factory=lambda: int(os.getenv("CC_CONTEXT_WINDOW", "180000"))
    )
    max_sessions: int = field(default_factory=lambda: int(os.getenv("CC_MAX_SESSIONS", "512")))
    environment: str = field(default_factory=lambda: os.getenv("CC_ENVIRONMENT", "development"))
    release: str = field(default_factory=lambda: os.getenv("CC_RELEASE", "dev"))
    api_bearer_token: str | None = field(
        default_factory=lambda: os.getenv("CC_API_BEARER_TOKEN") or None,
        repr=False,
    )
    executor_signing_private_key: str | None = field(
        default_factory=lambda: os.getenv("CC_EXECUTOR_SIGNING_PRIVATE_KEY") or None,
        repr=False,
    )
    max_concurrent_requests: int = field(
        default_factory=lambda: int(os.getenv("CC_MAX_CONCURRENT_REQUESTS", "16"))
    )

    def __post_init__(self) -> None:
        """Reject unsafe production configurations before traffic reaches the process."""
        if self.max_steps < 1 or self.max_sessions < 1 or self.max_concurrent_requests < 1:
            raise ValueError("Step, session, and concurrency limits must all be positive.")
        if self.environment not in {"development", "test", "production"}:
            raise ValueError(
                "CC_ENVIRONMENT must be development, test, or production; unknown values "
                "must not bypass production checks."
            )
        if self.llm_provider not in {"anthropic", "gemini", "openai"}:
            raise ValueError("CC_LLM_PROVIDER must be anthropic, gemini, or openai.")
        if self.environment != "production":
            return
        missing: list[str] = []
        if not self.api_bearer_token:
            missing.append("CC_API_BEARER_TOKEN")
        elif len(self.api_bearer_token) < 32:
            missing.append("CC_API_BEARER_TOKEN (at least 32 characters)")
        if not self.executor_url:
            missing.append("CC_EXECUTOR_URL")
        elif not self.executor_url.startswith("https://"):
            missing.append("CC_EXECUTOR_URL (HTTPS required)")
        if not self.executor_audience:
            missing.append("CC_EXECUTOR_AUDIENCE")
        if not self.executor_signing_private_key:
            missing.append("CC_EXECUTOR_SIGNING_PRIVATE_KEY")
        if self.llm_provider == "gemini" and not self.google_cloud_project:
            missing.append("GOOGLE_CLOUD_PROJECT")
        if self.release == "dev":
            missing.append("CC_RELEASE")
        if missing:
            raise ValueError(
                "Production configuration is unsafe. Set: " + ", ".join(sorted(missing))
            )


@dataclass
class Metrics:
    """Counters a dashboard can scrape. Deliberately small.

    Nothing here is a gauge over time, because that is the monitoring system's job. What the
    service owes it is honest counters, including the ones nobody wants to look at:
    `ungrounded_blocked` going up is the system working, and going to zero forever is the
    grounding gate having been switched off by accident.
    """

    requests: int = 0
    errors: int = 0
    answers: int = 0
    clarifications: int = 0
    cancelled: int = 0
    ungrounded_blocked: int = 0
    tool_failures: int = 0
    total_tokens: int = 0
    auth_rejected: int = 0
    overloaded: int = 0
    #: Bounded. An unbounded list is a slow leak and an increasingly expensive /metrics scrape
    #: (docs/AUDIT.md, P2-8). A thousand samples give the same percentiles.
    latency_ms: deque[float] = field(default_factory=lambda: deque(maxlen=1024))
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, **deltas: int) -> None:
        """Increment counters under a lock: the worker thread writes, the loop reads."""
        with self._lock:
            for name, delta in deltas.items():
                setattr(self, name, getattr(self, name) + delta)

    def observe_latency(self, value: float) -> None:
        """Record one request's wall time."""
        with self._lock:
            self.latency_ms.append(value)

    def snapshot(self) -> dict[str, Any]:
        """Current values, plus p50/p95 computed on read."""
        with self._lock:
            ordered = sorted(self.latency_ms)

        def pct(p: float) -> float:
            if not ordered:
                return 0.0
            return round(ordered[min(len(ordered) - 1, int(len(ordered) * p))], 2)

        return {
            "requests": self.requests,
            "errors": self.errors,
            "answers": self.answers,
            "clarifications": self.clarifications,
            "cancelled": self.cancelled,
            "ungrounded_blocked": self.ungrounded_blocked,
            "tool_failures": self.tool_failures,
            "total_tokens": self.total_tokens,
            "auth_rejected": self.auth_rejected,
            "overloaded": self.overloaded,
            "p50_latency_ms": pct(0.50),
            "p95_latency_ms": pct(0.95),
        }


class SessionStore:
    """Bounded, in-process, LRU conversation memory.

    Sessions used to be constructed per request and thrown away, so `session_id` was accepted,
    validated, threaded through, and dropped: turn two of every conversation started from
    nothing (docs/AUDIT.md, P0-3).

    This makes multi-turn work on **one** instance. It does not make it work behind an
    autoscaler: two Cloud Run instances do not share this dict, and a session pinned to the
    wrong one is a session that has forgotten. The fix is a Postgres-backed store, which the
    memory module was designed for and does not have. Stated here rather than discovered in
    production.
    """

    def __init__(self, factory: Callable[[str], ConversationMemory], max_sessions: int) -> None:
        """Build a store that evicts the least recently used session."""
        self._factory = factory
        self._max = max_sessions
        self._sessions: OrderedDict[str, ConversationMemory] = OrderedDict()
        self._turn_locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str) -> ConversationMemory:
        """Return the session's memory, creating it if this is turn one."""
        with self._lock:
            memory = self._sessions.get(session_id)
            if memory is None:
                memory = self._factory(session_id)
                self._sessions[session_id] = memory
                self._turn_locks[session_id] = threading.Lock()
            self._sessions.move_to_end(session_id)
            while len(self._sessions) > self._max:
                evicted, _ = self._sessions.popitem(last=False)
                self._turn_locks.pop(evicted, None)
            return memory

    def turn_lock(self, session_id: str) -> threading.Lock:
        """Serialize turns within one session.

        `SessionStore` was locked; the `ConversationMemory` it hands out was not. Two requests
        on the same `session_id` -- two browser tabs -- ran the agent concurrently against one
        memory, appending turns from two threads and letting `compress()` interleave with
        `render()` (docs/AUDIT.md, R4-2). Sessions are cheap to serialize: a conversation is
        sequential by definition.
        """
        with self._lock:
            return self._turn_locks.setdefault(session_id, threading.Lock())

    def __len__(self) -> int:
        """How many live sessions are held."""
        return len(self._sessions)


class ChatRequest(BaseModel):
    """One question."""

    question: str = Field(min_length=1, max_length=2000)
    session_id: str = Field(default="default", pattern=r"^[A-Za-z0-9_-]{1,64}$")


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event)}\n\n"


def build_tools(settings: Settings) -> dict[str, Any]:
    """Assemble the tool registry, choosing the local or the remote sandbox."""
    layer = SemanticLayer.load()
    warehouse = Warehouse(settings.warehouse_path)
    guard = SqlGuard(SqlGuardConfig(allowed_aggregates=layer.aggregate_atoms()))

    if settings.executor_url:
        from campaign_copilot.service.request_auth import RequestSigner
        from campaign_copilot.tools.remote_exec import RemoteSandbox, google_id_token_provider

        token_provider = (
            google_id_token_provider(settings.executor_audience)
            if settings.executor_audience
            else None
        )
        python_exec: Any = RemoteSandbox(
            base_url=settings.executor_url,
            token_provider=token_provider,
            signer=(
                RequestSigner.from_base64(settings.executor_signing_private_key)
                if settings.executor_signing_private_key
                else None
            ),
        )
    else:
        # In-process. Acceptable for local development only: see docs/threat-model.md.
        logger.warning("CC_EXECUTOR_URL unset; running python_exec in-process (not a boundary)")
        python_exec = PythonSandbox(config=SandboxConfig())

    return {
        "list_metrics": ListMetricsTool(layer=layer),
        "query_metrics": QueryMetricsTool(layer=layer, warehouse=warehouse),
        "run_sql": RunSqlTool(guard=guard, warehouse=warehouse),
        "search_docs": SearchDocsTool(HybridRetriever.build(build_corpus(layer))),
        "python_exec": python_exec,
    }


def create_app(
    settings: Settings | None = None,
    client_factory: Callable[[], LLMClient] | None = None,
    tools: dict[str, Any] | None = None,
) -> FastAPI:
    """Build the API. `client_factory` and `tools` are injected by the test suite."""
    configure_logging()
    config = settings or Settings()
    registry = tools if tools is not None else build_tools(config)
    metrics = Metrics()
    capacity = threading.BoundedSemaphore(config.max_concurrent_requests)

    def default_client() -> LLMClient:
        if config.llm_provider == "gemini":
            return GeminiClient(
                model=config.model,
                project=config.google_cloud_project,
                location=config.google_cloud_location,
            )
        if config.llm_provider == "openai":
            return OpenAIClient(model=config.model)
        return AnthropicClient(model=config.model)

    make_client = client_factory or default_client

    def new_memory(session_id: str) -> ConversationMemory:
        return ConversationMemory(
            session_id=session_id,
            budget=ContextBudget(context_window=config.context_window, max_output_tokens=2048),
        )

    sessions = SessionStore(new_memory, max_sessions=config.max_sessions)

    production = config.environment == "production"
    app = FastAPI(
        title="campaign-copilot",
        version="0.1.0",
        docs_url=None if production else "/docs",
        redoc_url=None if production else "/redoc",
        openapi_url=None if production else "/openapi.json",
    )
    app.state.metrics = metrics
    app.state.settings = config
    app.state.sessions = sessions

    @app.middleware("http")
    async def _request_context(request: Request, call_next: Any) -> Any:
        supplied_rid = request.headers.get("X-Request-ID", "")
        rid = supplied_rid if _REQUEST_ID.fullmatch(supplied_rid) else uuid.uuid4().hex[:16]
        token = request_id.set(rid)
        started = time.perf_counter()
        try:
            if request.url.path.startswith("/v1/") and config.api_bearer_token:
                supplied = request.headers.get("Authorization", "")
                scheme, separator, credential = supplied.partition(" ")
                authenticated = (
                    separator == " "
                    and scheme.lower() == "bearer"
                    and secrets.compare_digest(credential, config.api_bearer_token)
                )
                if not authenticated:
                    metrics.record(auth_rejected=1)
                    response = JSONResponse(
                        {"detail": "Authentication required", "request_id": rid},
                        status_code=401,
                        headers={"WWW-Authenticate": "Bearer"},
                    )
                else:
                    response = await call_next(request)
            else:
                response = await call_next(request)
        finally:
            request_id.reset(token)
        elapsed = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = rid
        logger.info(
            "request",
            extra={
                "extra_fields": {
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round(elapsed, 2),
                }
            },
        )
        return response

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """Liveness. Touches nothing: a dependency outage must not restart the process."""
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        """Readiness. The warehouse must answer; the executor, if configured, must answer."""
        checks: dict[str, str] = {}
        try:
            Warehouse(config.warehouse_path).execute("select 1")
            checks["warehouse"] = "ok"
        except Exception as err:
            checks["warehouse"] = f"error: {err}"

        if config.executor_url:
            import httpx

            try:
                headers = {}
                if config.executor_audience:
                    from campaign_copilot.tools.remote_exec import (
                        google_id_token_provider,
                    )

                    token = google_id_token_provider(config.executor_audience)()
                    if token:
                        headers["X-Serverless-Authorization"] = f"Bearer {token}"
                response = httpx.get(
                    f"{config.executor_url}/healthz", timeout=2.0, headers=headers
                )
                response.raise_for_status()
                checks["executor"] = "ok"
            except Exception as err:
                checks["executor"] = f"error: {err}"

        ready = all(v == "ok" for v in checks.values())
        return JSONResponse(
            {"ready": ready, "checks": checks}, status_code=200 if ready else 503
        )

    @app.get("/metrics")
    def read_metrics() -> dict[str, Any]:
        """Counters. `ungrounded_blocked` at zero forever means the gate is off."""
        return metrics.snapshot()

    @app.get("/v1/info")
    def info() -> dict[str, str]:
        """Immutable release identity for incident and model trace correlation."""
        return {
            "service": "campaign-copilot",
            "release": config.release,
            "provider": config.llm_provider,
            "model": config.model,
        }

    @app.post("/v1/chat")
    async def chat(body: ChatRequest) -> Response:
        """Stream the agent's decisions as server-sent events."""
        if not capacity.acquire(blocking=False):
            metrics.record(overloaded=1)
            return JSONResponse(
                {"detail": "Service is at capacity. Retry later."},
                status_code=429,
                headers={"Retry-After": "2"},
            )
        metrics.record(requests=1)
        rid = request_id.get()
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        # Flipped when the client disconnects. The worker polls it between steps and stops,
        # rather than running the whole plan for a client that has gone (docs/AUDIT.md, R5-1).
        cancel = threading.Event()

        def emit(event: dict[str, Any]) -> None:
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {
                    **event,
                    "request_id": rid,
                    "release": config.release,
                    "provider": config.llm_provider,
                    "model": config.model,
                },
            )

        def work() -> None:
            """Run the agent, streaming events, and always push the terminating sentinel.

            Everything is inside the `try`. A failure in the *setup* used to hang the stream
            forever: the sentinel lives in `finally`, and there was code above the `try`.
            """
            started = time.perf_counter()
            token = None
            try:
                # The sandbox reads the session from here, never from the model's arguments.
                token = session_scope.set(body.session_id)
                memory = sessions.get(body.session_id)
                # `remember` is injected per request, bound to this session's memory. It
                # must never live in the shared registry: a tool holding one session's
                # memory inside a registry shared by every session is a cross-session
                # write path (docs/AUDIT.md, P1-6b).
                agent = Agent(
                    make_client(),
                    {**registry, "remember": RememberTool(memory=memory)},
                    memory,
                    checker=GroundingChecker(),
                    max_steps=config.max_steps,
                )
                # A conversation is sequential. Two tabs on one session must not interleave.
                with sessions.turn_lock(body.session_id):
                    result = agent.run(body.question, on_event=emit, is_cancelled=cancel.is_set)
                metrics.record(
                    total_tokens=result.trace.usage.total_tokens,
                    tool_failures=sum(1 for s in result.trace.steps if s.tool and not s.ok),
                )
                if result.cancelled:
                    metrics.record(cancelled=1)
                elif result.needs_clarification:
                    metrics.record(clarifications=1)
                elif result.ok:
                    metrics.record(answers=1)
                else:
                    metrics.record(errors=1)
                    if result.trace.grounding and not result.trace.grounding.ok:
                        metrics.record(ungrounded_blocked=1)
                emit({"type": "done", "ok": result.ok, "answer": result.answer})
            except Exception:
                metrics.record(errors=1)
                logger.exception("agent failed")
                emit(
                    {
                        "type": "error",
                        "reason": "internal",
                        "text": "Internal error. Use the request id to locate the server log.",
                    }
                )
            finally:
                if token is not None:
                    session_scope.reset(token)
                metrics.observe_latency((time.perf_counter() - started) * 1000)
                loop.call_soon_threadsafe(queue.put_nowait, None)

        async def stream() -> AsyncIterator[str]:
            task = asyncio.create_task(asyncio.to_thread(work))
            try:
                while (event := await queue.get()) is not None:
                    yield _sse(event)
            except asyncio.CancelledError:
                # Starlette cancels this generator when the client disconnects. Signal the
                # worker to stop at its next step, then let it drain rather than orphaning it.
                cancel.set()
                raise
            finally:
                cancel.set()
                try:
                    await task
                finally:
                    capacity.release()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
