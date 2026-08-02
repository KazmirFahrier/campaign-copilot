"""The `remember` tool: the write path for pinned facts.

`ConversationMemory.pin()` existed, was tested, and was reachable from nothing in `src/`
(docs/AUDIT.md, P1-6b). A standing instruction like "exclude branded search from now on"
lived or died on the rolling summary keeping it, which is exactly the failure pinning was
built to prevent. This tool is the missing call path: the model pins the constraint the
moment the user states it, and the pinned block outlives every compression.

Two design rules, both defensive:

* **A pinned fact licenses no numbers.** "Use the last 28 days" pins ``28`` into the
  system prompt; that must not entitle the agent to state 28 in an answer. Results are
  built with :meth:`ToolResult.reference`, so :mod:`campaign_copilot.grounding` ignores
  them, and ``queries_run`` stays false -- remembering is not research.
* **The pin store is capped.** Pinned facts are injected into the system prompt on every
  turn. An unbounded store is a prompt-stuffing channel: a document that persuades the
  model to pin a paragraph 50 times owns the context window. Caps make the worst case
  boring (see docs/failure-modes.md, #7).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from campaign_copilot.memory import ConversationMemory
from campaign_copilot.tools.base import ToolResult, ToolSpec

__all__ = ["MAX_KEY_CHARS", "MAX_PINNED_FACTS", "MAX_VALUE_CHARS", "RememberTool"]

MAX_PINNED_FACTS = 16
MAX_KEY_CHARS = 64
MAX_VALUE_CHARS = 200


@dataclass
class RememberTool:
    """Pin a standing constraint into session memory, or retract one.

    Holds the *same* :class:`ConversationMemory` the agent renders from. The service
    injects one per request, bound to the session's memory; the shared tool registry
    never contains it, because a tool bound to one session inside a registry shared by
    all of them is a cross-session write path.
    """

    memory: ConversationMemory

    spec: ClassVar[ToolSpec] = ToolSpec(
        name="remember",
        description=(
            "Pin a standing constraint the user has established, so it survives for the "
            "rest of the conversation (e.g. a date range, an exclusion, a definition). "
            "Use it the moment the user states a constraint that applies 'from now on'. "
            "Set forget=true to retract a pin the user has withdrawn. Do not pin query "
            "results or numbers you computed: pins are instructions, not findings."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Short snake_case name for the fact, e.g. 'date_range'.",
                },
                "value": {
                    "type": "string",
                    "description": "The constraint, in the user's own words where possible.",
                },
                "forget": {
                    "type": "boolean",
                    "description": "Retract this key instead of pinning it.",
                },
            },
            "required": ["key"],
        },
    )

    def run(self, **kwargs: Any) -> ToolResult:
        """Pin or retract. Failures are results the model can repair from, never raises."""
        key = str(kwargs.get("key") or "").strip()
        forget = bool(kwargs.get("forget", False))
        raw_value = kwargs.get("value")

        if not key:
            return ToolResult.failure("INVALID_KEY", "`key` must be a non-empty string.")
        if len(key) > MAX_KEY_CHARS:
            return ToolResult.failure(
                "INVALID_KEY", f"`key` is longer than {MAX_KEY_CHARS} characters."
            )

        if forget:
            self.memory.unpin(key)
            return ToolResult.reference(f"Forgot {key!r}.", key=key, forgotten=True)

        value = str(raw_value or "").strip()
        if not value:
            return ToolResult.failure(
                "MISSING_VALUE", "`value` is required unless forget=true."
            )
        if len(value) > MAX_VALUE_CHARS:
            return ToolResult.failure(
                "VALUE_TOO_LONG",
                f"`value` is {len(value)} characters; the cap is {MAX_VALUE_CHARS}. "
                "Pin the constraint, not the essay.",
            )
        if key not in self.memory.pinned and len(self.memory.pinned) >= MAX_PINNED_FACTS:
            return ToolResult.failure(
                "PIN_LIMIT",
                f"{MAX_PINNED_FACTS} facts are already pinned. Forget one first.",
            )

        self.memory.pin(key, value)
        return ToolResult.reference(
            f"Pinned {key!r} = {value!r}. It will be honoured for the rest of this "
            "conversation.",
            key=key,
            value=value,
        )
