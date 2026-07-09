"""Multi-turn memory.

Three tiers, because a single tier always fails one of the three tests in the Phase 4
``multi_turn.jsonl`` suite:

* **Verbatim buffer** -- the last few turns, unmodified. Pronoun resolution ("now break
  *that* out by channel") needs the literal previous turn, not a summary of it.
* **Rolling summary** -- everything older, compressed by the model. Cheap, lossy, and
  sufficient for "as I said earlier, we're looking at Q4".
* **Pinned facts** -- explicit key/value state the agent writes, e.g. ``date_range =
  last 28 days`` or ``exclude_branded = true``. These are *never* evicted. A summary
  that quietly drops "exclude branded search" produces an answer that is wrong in the
  one way the user specifically asked it not to be.

Compression is triggered by the budget, not by a turn count, because a single 400-row
tool result costs more context than ten conversational turns.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from campaign_copilot.llm.client import Message
from campaign_copilot.llm.tokens import ContextBudget, FitResult

__all__ = ["ConversationMemory", "Summarizer"]

#: Compresses a run of old messages into a paragraph.
Summarizer = Callable[[Sequence[Message]], str]

_PINNED_HEADER = (
    "Facts established earlier in this conversation. They override any default and "
    "must be respected without being restated:"
)


@dataclass(slots=True)
class ConversationMemory:
    """Session state for one conversation."""

    session_id: str
    budget: ContextBudget
    verbatim_turns: int = 6
    summary: str = ""
    pinned: dict[str, str] = field(default_factory=dict)
    turns: list[Message] = field(default_factory=list)

    # ------------------------------------------------------------------ mutation

    def add(self, message: Message) -> None:
        """Append a turn."""
        self.turns.append(message)

    def pin(self, key: str, value: str) -> None:
        """Record a fact that must survive every future compression."""
        self.pinned[key] = value

    def unpin(self, key: str) -> None:
        """Forget a pinned fact. Silent if absent, because the agent may retract twice."""
        self.pinned.pop(key, None)

    # ---------------------------------------------------------------- rendering

    def pinned_block(self) -> str:
        """The pinned facts, rendered for the system prompt. Empty when nothing is pinned."""
        if not self.pinned:
            return ""
        lines = "\n".join(f"- {k}: {v}" for k, v in sorted(self.pinned.items()))
        return f"{_PINNED_HEADER}\n{lines}"

    def summary_message(self) -> Message | None:
        """The rolling summary as a message, or ``None`` if nothing has been compressed."""
        if not self.summary:
            return None
        return Message(
            role="user",
            content=f"Summary of the earlier part of this conversation:\n{self.summary}",
        )

    def compress(self, summarize: Summarizer) -> int:
        """Fold everything older than the verbatim window into the rolling summary.

        Returns:
            How many turns were compressed. Zero when the buffer is already short.
        """
        if len(self.turns) <= self.verbatim_turns:
            return 0
        old, recent = self.turns[: -self.verbatim_turns], self.turns[-self.verbatim_turns :]
        to_summarize = [*([self.summary_message()] if self.summary else []), *old]
        self.summary = summarize([m for m in to_summarize if m is not None])
        self.turns = recent
        return len(old)

    def render(self, system: str, *fixed: str) -> tuple[str, FitResult]:
        """Build the system prompt and the fitted message list for the next call.

        The final user turn and the rolling summary are non-evictable: dropping the
        question defeats the purpose, and dropping the summary silently loses every
        earlier turn at once rather than one at a time.
        """
        pinned = self.pinned_block()
        full_system = f"{system}\n\n{pinned}" if pinned else system

        history: list[Message] = []
        summary = self.summary_message()
        if summary is not None:
            history.append(summary)
        history.extend(self.turns)

        must_keep: list[Message] = [m for m in (summary,) if m is not None]
        last_user = next((m for m in reversed(self.turns) if m.role == "user"), None)
        if last_user is not None:
            must_keep.append(last_user)

        result = self.budget.fit(history, full_system, *fixed, must_keep=must_keep)
        return full_system, result

    # ------------------------------------------------------------------- helpers

    @property
    def needs_compression(self) -> bool:
        """True when history no longer fits and there is something old to fold away."""
        if len(self.turns) <= self.verbatim_turns:
            return False
        cost = sum(self.budget.measure(m) for m in self.turns)
        return cost > self.budget.available_for_history()
