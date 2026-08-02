"""The agent's view of the executor service.

Identical `spec` and identical `ToolResult` contract to the in-process
:class:`~campaign_copilot.tools.python_exec.PythonSandbox`. The agent cannot tell them apart,
which is the point: the security boundary is a deployment decision, not an application one,
and swapping it must not change a line of the loop.

Failure modes get error codes the model can act on, because a network hiccup reaching the
executor and a `ZeroDivisionError` inside it are different problems and the agent should not
respond to them the same way.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

import httpx

from campaign_copilot.tools.base import ToolResult, ToolSpec
from campaign_copilot.tools.python_exec import PythonSandbox, session_scope

__all__ = ["RemoteSandbox", "google_id_token_provider"]

#: Returns a bearer token for the executor, or ``None`` when the executor is unauthenticated.
TokenProvider = Callable[[], str | None]


def google_id_token_provider(audience: str) -> TokenProvider:
    """Fetch an OIDC identity token for a Cloud Run service.

    The Terraform sets the executor to `INGRESS_TRAFFIC_INTERNAL_ONLY` and grants the api's
    service account `roles/run.invoker`. Cloud Run enforces that binding by demanding an
    identity token on every request. An earlier version of this client sent none, so the first
    `python_exec` call in production would have returned 403 and `/readyz` would have reported
    the executor down forever (docs/AUDIT.md, P0-2).

    `terraform validate` passing said nothing about whether the two services could talk.
    """

    def provide() -> str | None:
        import google.auth.transport.requests
        import google.oauth2.id_token

        request = google.auth.transport.requests.Request()
        # google-auth does not currently publish a typed signature for this helper.
        token: str = google.oauth2.id_token.fetch_id_token(request, audience)
        return token

    return provide


@dataclass
class RemoteSandbox:
    """Executes Python in the executor service over HTTP."""

    base_url: str
    timeout_seconds: float = 30.0
    client: httpx.Client | None = field(default=None, repr=False)
    token_provider: TokenProvider | None = field(default=None, repr=False)

    #: Deliberately the same spec object the in-process sandbox advertises. If these ever
    #: diverge, the model is being told a different story depending on how we deployed.
    spec: ClassVar[ToolSpec] = PythonSandbox.spec

    def _http(self) -> httpx.Client:
        if self.client is None:
            self.client = httpx.Client(base_url=self.base_url, timeout=self.timeout_seconds)
        return self.client

    def _headers(self) -> dict[str, str]:
        """Attach the identity token Cloud Run's invoker binding requires."""
        if self.token_provider is None:
            return {}
        token = self.token_provider()
        return {"X-Serverless-Authorization": f"Bearer {token}"} if token else {}

    def run(self, **kwargs: Any) -> ToolResult:
        """POST the cell to the executor and translate its verdict back into a ToolResult."""
        # The session id is read from server state, never from the model's arguments.
        payload = {"code": kwargs.get("code", ""), "session_id": session_scope.get()}
        try:
            response = self._http().post("/exec", json=payload, headers=self._headers())
            response.raise_for_status()
        except httpx.TimeoutException:
            return ToolResult.failure(
                "EXECUTOR_TIMEOUT",
                "The executor did not respond in time. The code may still be running; do "
                "not retry the same cell.",
            )
        except httpx.HTTPError as err:
            # An infrastructure failure, not a bug in the model's code. Saying so stops the
            # agent from "repairing" perfectly good Python.
            return ToolResult.failure(
                "EXECUTOR_UNAVAILABLE", f"Could not reach the executor: {err}"
            )

        body = response.json()
        if body["ok"]:
            return ToolResult.success(body["content"], **body.get("data", {}))
        # Rebuild rather than call .failure(): the executor already prefixed the code onto
        # the content, and prefixing it twice is how "TIMEOUT: TIMEOUT: ..." reaches a model.
        return ToolResult(
            ok=False,
            content=body["content"],
            error_code=body.get("error_code") or "EXECUTION_ERROR",
        )

    def reset(self, session_id: str = "default") -> None:
        """Drop a session's namespace in the executor."""
        self._http().post(
            "/reset", json={"code": "", "session_id": session_id}, headers=self._headers()
        )
