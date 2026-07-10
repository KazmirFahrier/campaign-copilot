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

from dataclasses import dataclass, field
from typing import Any, ClassVar

import httpx

from campaign_copilot.tools.base import ToolResult, ToolSpec
from campaign_copilot.tools.python_exec import PythonSandbox

__all__ = ["RemoteSandbox"]


@dataclass
class RemoteSandbox:
    """Executes Python in the executor service over HTTP."""

    base_url: str
    timeout_seconds: float = 30.0
    client: httpx.Client | None = field(default=None, repr=False)

    #: Deliberately the same spec object the in-process sandbox advertises. If these ever
    #: diverge, the model is being told a different story depending on how we deployed.
    spec: ClassVar[ToolSpec] = PythonSandbox.spec

    def _http(self) -> httpx.Client:
        if self.client is None:
            self.client = httpx.Client(base_url=self.base_url, timeout=self.timeout_seconds)
        return self.client

    def run(self, **kwargs: Any) -> ToolResult:
        """POST the cell to the executor and translate its verdict back into a ToolResult."""
        payload = {
            "code": kwargs.get("code", ""),
            "session_id": str(kwargs.get("session_id", "default")),
        }
        try:
            response = self._http().post("/exec", json=payload)
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
        self._http().post("/reset", json={"code": "", "session_id": session_id})
