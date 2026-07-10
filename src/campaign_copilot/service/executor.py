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

from fastapi import FastAPI
from pydantic import BaseModel, Field

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


def create_app(sandbox: PythonSandbox | None = None) -> FastAPI:
    """Build the executor app. `sandbox` is injectable so the suite can shrink the limits."""
    assert_no_secrets()
    engine = sandbox or PythonSandbox(config=SandboxConfig())
    app = FastAPI(title="campaign-copilot executor", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
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
