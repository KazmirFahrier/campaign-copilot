"""The executor service.

`docs/threat-model.md` has said, since Phase 2, that `python_exec` is a resource limiter and
not a security boundary: it runs as the same UID as the agent, and anything executing there
can read `/proc/self/environ` and therefore the LLM API key. The document's answer was "the
real boundary is the container in Phase 6." This is that container.

The architecture is one seam, not one flag:

    ┌──────────────┐   HTTP    ┌──────────────────┐
    │  api service │ ────────► │ executor service │
    │  holds keys  │           │  holds NOTHING   │
    │  has egress  │           │  no egress       │
    └──────────────┘           └──────────────────┘

The executor has no LLM credentials, no warehouse credentials, no network egress, and a
distinct service account with no IAM bindings. Arbitrary code execution inside it buys the
attacker a container with nothing in it.

That is a deployment property, and deployment properties rot silently. So it is also asserted
in code: :func:`assert_no_secrets` runs at import and the process **refuses to start** if a
credential is visible in its environment. A misconfigured Cloud Run revision that mounts the
API's secrets into the executor does not quietly become insecure -- it crash-loops, which is
what you want a misconfiguration to do.
"""

from __future__ import annotations

import os
import re
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from campaign_copilot.service.request_auth import RequestVerifier
from campaign_copilot.tools.python_exec import PythonSandbox, SandboxConfig

__all__ = [
    "ExecRequest",
    "ExecResponse",
    "SecretsVisibleError",
    "assert_no_secrets",
    "create_app",
]

#: Anything matching these is a credential, and the executor may not see one.
_SECRET_PATTERN = re.compile(r"(API_KEY|SECRET|TOKEN|PASSWORD|CREDENTIALS|_PRIVATE_KEY)$", re.I)

#: Cloud Run injects these. They are not credentials.
_ALLOWED = frozenset(
    {
        "PATH",
        "HOME",
        "HOSTNAME",
        "PORT",
        "K_SERVICE",
        "K_REVISION",
        "K_CONFIGURATION",
        "PYTHONPATH",
        "LANG",
        "LC_ALL",
        "TZ",
    }
)


class SecretsVisibleError(RuntimeError):
    """The executor can see a credential. It must not run."""


def assert_no_secrets(environ: dict[str, str] | None = None) -> None:
    """Refuse to start if any credential is visible.

    The executor's whole value is that arbitrary code execution inside it is uninteresting.
    A single mounted secret destroys that, silently, and nothing else in the system would
    notice. So it is checked, loudly, at startup.
    """
    env = os.environ if environ is None else environ
    visible = sorted(k for k in env if _SECRET_PATTERN.search(k) and k not in _ALLOWED)
    if visible:
        raise SecretsVisibleError(
            "The executor can see credentials in its environment: "
            f"{visible}. It runs untrusted code and must hold nothing. "
            "Remove these bindings from the executor's service account and revision."
        )


class ExecRequest(BaseModel):
    """One cell of agent-authored Python."""

    code: str
    session_id: str = Field(default="default", max_length=64)


class ExecResponse(BaseModel):
    """The sandbox's verdict, serialized."""

    ok: bool
    content: str
    data: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None


def create_app(
    sandbox: PythonSandbox | None = None, environ: dict[str, str] | None = None
) -> FastAPI:
    """Build the executor app.

    `environ` is injectable because `assert_no_secrets()` reads the real process environment,
    and every developer with `ANTHROPIC_API_KEY` exported could not run the test suite
    (docs/AUDIT.md, R2-5). A safety check that makes the tests fail is a safety check somebody
    weakens. The production path still reads `os.environ`.
    """
    assert_no_secrets(environ)
    engine = sandbox or PythonSandbox(config=SandboxConfig())
    app = FastAPI(title="campaign-copilot executor", docs_url=None, redoc_url=None)
    environment = os.environ if environ is None else environ
    public_key = environment.get("CC_EXECUTOR_SIGNING_PUBLIC_KEY")
    verifier = RequestVerifier.from_base64(public_key) if public_key else None

    @app.middleware("http")
    async def authenticate_execution(request: Request, call_next: Any) -> Any:
        if request.url.path not in {"/exec", "/reset"} or verifier is None:
            return await call_next(request)
        body = await request.body()
        valid = verifier.verify(
            timestamp=request.headers.get("X-CC-Timestamp", ""),
            nonce=request.headers.get("X-CC-Nonce", ""),
            signature=request.headers.get("X-CC-Signature", ""),
            method=request.method,
            path=request.url.path,
            body=body,
        )
        if not valid:
            return JSONResponse({"detail": "Authentication required"}, status_code=401)
        return await call_next(request)

    @app.get("/health")
    @app.get("/healthz", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/exec", response_model=ExecResponse)
    def execute(request: ExecRequest) -> ExecResponse:
        result = engine.run(code=request.code, session_id=request.session_id)
        return ExecResponse(
            ok=result.ok,
            content=result.content,
            data=result.data,
            error_code=result.error_code,
        )

    @app.post("/reset")
    def reset(request: ExecRequest) -> dict[str, str]:
        engine.reset(request.session_id)
        return {"status": "reset"}

    return app
