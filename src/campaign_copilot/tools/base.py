"""The tool contract.

One rule shapes everything here: **a tool failure is a result, not an exception.**

When the guardrail rejects a query, the agent needs to see *why*, in a form it can act
on, so that its single recovery attempt is a repair rather than a re-roll. A raised
exception either crashes the loop or gets stringified into something the model cannot
parse. So every tool returns a :class:`ToolResult`, and failures carry a machine-readable
``error_code`` -- the same codes the SQL guardrail already emits.

The loop grants exactly one recovery attempt per tool call. Two consecutive failures of
the same tool escalate to the user. An agent that retries a failing tool indefinitely is
the single most expensive bug in this class of system.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = ["Tool", "ToolResult", "ToolSpec"]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """What the model is told about a tool. Serialized into the request's tool schema."""

    name: str
    description: str
    input_schema: dict[str, Any]

    def as_anthropic(self) -> dict[str, Any]:
        """Render in the Anthropic tool-use shape."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    def as_openai(self) -> dict[str, Any]:
        """Render in the OpenAI function-calling shape."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The outcome of one tool call.

    ``content`` is what the model reads. ``data`` is the structured payload the rest of
    the system reads -- in particular :mod:`campaign_copilot.grounding`, which will not
    let the agent state a number that does not appear in some ``data`` here.
    """

    ok: bool
    content: str
    data: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None

    @classmethod
    def failure(cls, code: str, message: str) -> ToolResult:
        """Build a failure the model can repair from."""
        return cls(ok=False, content=f"{code}: {message}", error_code=code)

    @classmethod
    def success(cls, content: str, **data: Any) -> ToolResult:
        """Build a success carrying a structured payload."""
        return cls(ok=True, content=content, data=data)

    def as_message_content(self) -> str:
        """What gets appended to the conversation as the tool's turn."""
        return self.content

    def numeric_facts(self) -> list[float]:
        """Every number this result licenses the agent to state.

        Walks ``data`` recursively so a nested result set still grounds its own values.
        Booleans are excluded: ``True`` is not the number 1 in any answer worth checking.
        """
        found: list[float] = []

        def walk(node: Any) -> None:
            if isinstance(node, bool):
                return
            if isinstance(node, (int, float)):
                found.append(float(node))
            elif isinstance(node, dict):
                for value in node.values():
                    walk(value)
            elif isinstance(node, (list, tuple)):
                for value in node:
                    walk(value)

        walk(self.data)
        return found

    def __str__(self) -> str:
        """Compact rendering for logs and traces."""
        status = "ok" if self.ok else f"error[{self.error_code}]"
        return f"<ToolResult {status} {json.dumps(self.content[:80])}>"


@runtime_checkable
class Tool(Protocol):
    """Anything the agent may call."""

    spec: ToolSpec

    def run(self, **kwargs: Any) -> ToolResult:
        """Execute the tool. Must not raise for expected failures."""
        ...
