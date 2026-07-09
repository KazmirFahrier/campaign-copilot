"""Provider-agnostic LLM interface.

The rest of the codebase depends on :class:`LLMClient`, never on a vendor SDK. Three
consequences, all of them deliberate:

* The evaluation harness can swap in :class:`ScriptedClient` and run the whole agent
  loop deterministically, offline, in CI, with no key and no cost.
* Swapping Anthropic for OpenAI is a config change, which is what the JD means by
  "experience with LLM APIs" rather than "experience with one LLM API".
* Vendor SDKs are imported lazily inside the adapter's ``__init__``. Installing this
  package does not require either of them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

__all__ = [
    "AnthropicClient",
    "LLMClient",
    "LLMResponse",
    "Message",
    "OpenAIClient",
    "Role",
    "ScriptExhaustedError",
    "ScriptedClient",
    "Usage",
]

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True, slots=True)
class Message:
    """One turn. ``content`` is text; tool payloads are serialized by the caller."""

    role: Role
    content: str
    name: str | None = None
    tool_call_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Render in the shape both vendor SDKs accept for plain text turns."""
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True, slots=True)
class Usage:
    """Token accounting for one call. Summed across a session for cost reporting."""

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """Input plus output."""
        return self.input_tokens + self.output_tokens

    def __add__(self, other: Usage) -> Usage:
        """Accumulate usage across calls."""
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
        )


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """A single completion."""

    text: str
    model: str
    usage: Usage = field(default_factory=Usage)
    stop_reason: str | None = None


@runtime_checkable
class LLMClient(Protocol):
    """The only surface the agent is allowed to call."""

    model: str

    def complete(
        self,
        messages: Sequence[Message],
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """Return one completion for ``messages``."""
        ...

    def count_tokens(self, text: str) -> int:
        """Count tokens in ``text`` the way this provider will bill them."""
        ...


# --------------------------------------------------------------------------- fake


class ScriptExhaustedError(RuntimeError):
    """The scripted client ran out of canned responses.

    In a test this almost always means the code under test called the model more times
    than the test expected -- which is exactly the bug worth failing on.
    """


@dataclass
class ScriptedClient:
    """A deterministic client that replays a fixed list of responses.

    This is what makes the Phase 4 regression suite possible: an agent trace is a
    sequence of model outputs, and a recorded trace replays byte-for-byte.
    """

    responses: list[str]
    model: str = "scripted"
    calls: list[list[Message]] = field(default_factory=list)
    systems: list[str | None] = field(default_factory=list)
    _index: int = 0

    def complete(
        self,
        messages: Sequence[Message],
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """Pop the next canned response."""
        if self._index >= len(self.responses):
            raise ScriptExhaustedError(
                f"Model called {self._index + 1} times but only "
                f"{len(self.responses)} responses were scripted."
            )
        text = self.responses[self._index]
        self._index += 1
        self.calls.append(list(messages))
        self.systems.append(system)
        return LLMResponse(
            text=text,
            model=self.model,
            usage=Usage(
                self.count_tokens(" ".join(m.content for m in messages)),
                self.count_tokens(text),
            ),
        )

    def count_tokens(self, text: str) -> int:
        """Cheap deterministic count; see :mod:`campaign_copilot.llm.tokens`."""
        return max(1, len(text) // 4)

    @property
    def call_count(self) -> int:
        """How many times the model was invoked."""
        return self._index


# ----------------------------------------------------------------------- adapters


class AnthropicClient:
    """Adapter for the Anthropic Messages API."""

    def __init__(self, model: str = "claude-sonnet-4-5", api_key: str | None = None) -> None:
        """Import the SDK lazily so the package installs without it."""
        import anthropic

        self._client = (
            anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        )
        self.model = model

    def complete(
        self,
        messages: Sequence[Message],
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """Call the Messages API and flatten the content blocks to text."""
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [m.as_dict() for m in messages if m.role != "system"],
        }
        if system:
            kwargs["system"] = system
        resp = self._client.messages.create(**kwargs)
        text = "".join(block.text for block in resp.content if block.type == "text")
        return LLMResponse(
            text=text,
            model=self.model,
            usage=Usage(resp.usage.input_tokens, resp.usage.output_tokens),
            stop_reason=resp.stop_reason,
        )

    def count_tokens(self, text: str) -> int:
        """Use the provider's own counter; it is the number that gets billed."""
        resp = self._client.messages.count_tokens(
            model=self.model, messages=[{"role": "user", "content": text}]
        )
        return int(resp.input_tokens)


class OpenAIClient:
    """Adapter for the OpenAI Chat Completions API."""

    def __init__(self, model: str = "gpt-4o", api_key: str | None = None) -> None:
        """Import the SDK lazily so the package installs without it."""
        import openai

        self._client = openai.OpenAI(api_key=api_key) if api_key else openai.OpenAI()
        self.model = model

    def complete(
        self,
        messages: Sequence[Message],
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """Call chat completions, prepending ``system`` as a system turn."""
        payload = [m.as_dict() for m in messages]
        if system:
            payload = [{"role": "system", "content": system}, *payload]
        resp = self._client.chat.completions.create(
            model=self.model, messages=payload, max_tokens=max_tokens, temperature=temperature
        )
        usage = resp.usage
        return LLMResponse(
            text=resp.choices[0].message.content or "",
            model=self.model,
            usage=Usage(usage.prompt_tokens, usage.completion_tokens) if usage else Usage(),
            stop_reason=resp.choices[0].finish_reason,
        )

    def count_tokens(self, text: str) -> int:
        """Count with ``tiktoken`` when available; fall back to the heuristic."""
        try:
            import tiktoken
        except ImportError:
            return max(1, len(text) // 4)
        return len(tiktoken.encoding_for_model(self.model).encode(text))
