"""Context-window arithmetic.

Two failure modes this exists to prevent, both of which are quiet:

1. **Silent truncation at the provider.** Send more than the window and the API errors,
   or worse, some frameworks trim for you and the model answers from a conversation it
   only half saw. Here, eviction is explicit, logged, and returned to the caller.

2. **Optimistic counting.** ``len(text) // 4`` under-counts on SQL, JSON and non-Latin
   text -- exactly the content this system sends. :class:`HeuristicCounter` deliberately
   *over*-estimates so a budget check that passes offline also passes at the provider.
   When a real counter is available, use it; the heuristic is the fallback, not the plan.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from campaign_copilot.llm.client import Message

__all__ = [
    "ContextBudget",
    "ContextOverflowError",
    "FitResult",
    "HeuristicCounter",
    "TokenCounter",
]

#: Per-message framing overhead (role, delimiters). Small, but 40 turns of it is not.
MESSAGE_OVERHEAD_TOKENS = 4


class TokenCounter(Protocol):
    """Anything that can count tokens the way the target provider bills them."""

    def count(self, text: str) -> int:
        """Return the token count of ``text``."""
        ...


@dataclass(frozen=True, slots=True)
class HeuristicCounter:
    """Offline, deterministic, and pessimistic by construction.

    English prose averages ~4 characters per token. SQL, JSON and identifiers such as
    ``paid_search_nonbrand`` average closer to 3. Dividing by 3.2 therefore over-counts
    prose and roughly matches code, so a budget that fits here fits in production.
    """

    chars_per_token: float = 3.2

    def count(self, text: str) -> int:
        """Over-estimate the token count of ``text``."""
        return max(1, int(len(text) / self.chars_per_token) + 1)


class ContextOverflowError(RuntimeError):
    """Even the messages that may never be evicted do not fit in the window."""


@dataclass(frozen=True, slots=True)
class FitResult:
    """What survived the budget, and what did not."""

    kept: tuple[Message, ...]
    evicted: tuple[Message, ...]
    tokens_used: int
    tokens_available: int

    @property
    def evicted_any(self) -> bool:
        """True when the budget forced something out of context."""
        return bool(self.evicted)


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """Decides how much history fits alongside the fixed costs of a turn.

    ``reserve_fraction`` is headroom against counter error and against the model
    producing a longer answer than ``max_output_tokens`` nominally allows.
    """

    context_window: int
    max_output_tokens: int = 1024
    reserve_fraction: float = 0.10
    counter: TokenCounter = HeuristicCounter()

    def __post_init__(self) -> None:
        """Reject nonsense budgets at construction rather than mid-conversation."""
        if not 0.0 <= self.reserve_fraction < 1.0:
            raise ValueError("reserve_fraction must be in [0, 1)")
        if self.max_output_tokens >= self.context_window:
            raise ValueError("max_output_tokens must be smaller than context_window")

    def measure(self, message: Message) -> int:
        """Tokens a single message consumes, framing included."""
        return self.counter.count(message.content) + MESSAGE_OVERHEAD_TOKENS

    def available_for_history(self, *fixed: str) -> int:
        """Budget left for conversation history after the fixed costs of this turn.

        ``fixed`` is everything that cannot be evicted and is not a history message:
        the system prompt, the serialized tool schemas, the retrieved RAG context.
        """
        reserve = int(self.context_window * self.reserve_fraction)
        fixed_cost = sum(self.counter.count(f) for f in fixed if f)
        return self.context_window - self.max_output_tokens - reserve - fixed_cost

    def fit(
        self,
        history: Sequence[Message],
        *fixed: str,
        must_keep: Sequence[Message] = (),
    ) -> FitResult:
        """Drop the oldest history until it fits, never touching ``must_keep``.

        ``must_keep`` is for pinned facts and the current user question. If those alone
        exceed the budget the conversation cannot proceed and we say so loudly rather
        than returning a silently mutilated context.
        """
        available = self.available_for_history(*fixed)
        floor = sum(self.measure(m) for m in must_keep)
        if floor > available:
            raise ContextOverflowError(
                f"Non-evictable messages need {floor} tokens but only {available} are "
                f"available in a {self.context_window}-token window. Reduce the system "
                "prompt, the tool schemas, or the retrieved context."
            )

        # Identity, not equality. Two turns can be textually identical -- a user who
        # asks the same question twice -- and evicting both because one was chosen
        # would be a silent, very confusing context bug.
        protected = {id(m) for m in must_keep}
        used = floor
        kept_ids: set[int] = set()
        evicted: list[Message] = []

        # Newest-first: recency is what a follow-up question depends on.
        for message in reversed(history):
            if id(message) in protected:
                continue
            cost = self.measure(message)
            if used + cost <= available:
                used += cost
                kept_ids.add(id(message))
            else:
                evicted.append(message)

        ordered = tuple(m for m in history if id(m) in kept_ids or id(m) in protected)
        return FitResult(
            kept=ordered,
            evicted=tuple(reversed(evicted)),
            tokens_used=used,
            tokens_available=available,
        )
