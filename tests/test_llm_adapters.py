"""The real LLM adapters, tested against mocked SDKs.

`ScriptedClient` skips the one thing the adapters exist to do: translate provider-specific
response shapes into an `LLMResponse`. That translation -- the content-block flattening
for Anthropic, `resp.choices[0].message.content` for OpenAI -- ran only against a real API key,
so every deterministic test in the suite validated a path the production clients never take
(docs/AUDIT.md, R6-1).

These tests install a fake `anthropic` / `openai` module, drive the adapter, and assert both the
request it builds and the `LLMResponse` it returns. No network, no key.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from typing import Any

import pytest

from campaign_copilot.llm.client import Message

# --------------------------------------------------------------------- gemini


@pytest.fixture
def gemini_capture() -> Iterator[dict[str, Any]]:
    captured: dict[str, Any] = {}

    class GenerateContentConfig:
        def __init__(self, **kwargs: Any) -> None:
            captured["config"] = kwargs

    class FakeModels:
        def generate_content(self, **kwargs: Any) -> Any:
            captured["generate"] = kwargs
            return types.SimpleNamespace(
                text='{"action": "answer"}',
                usage_metadata=types.SimpleNamespace(
                    prompt_token_count=13, candidates_token_count=5
                ),
                candidates=[types.SimpleNamespace(finish_reason="STOP")],
            )

        def count_tokens(self, **kwargs: Any) -> Any:
            captured["count"] = kwargs
            return types.SimpleNamespace(total_tokens=17)

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            captured["init"] = kwargs
            self.models = FakeModels()

    google = types.ModuleType("google")
    google.__path__ = []  # type: ignore[attr-defined]
    genai = types.ModuleType("google.genai")
    genai.Client = FakeClient  # type: ignore[attr-defined]
    genai.types = types.SimpleNamespace(GenerateContentConfig=GenerateContentConfig)  # type: ignore[attr-defined]
    google.genai = genai  # type: ignore[attr-defined]
    saved = {name: sys.modules.get(name) for name in ("google", "google.genai")}
    sys.modules["google"] = google
    sys.modules["google.genai"] = genai
    try:
        yield captured
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def test_gemini_adapter_uses_workload_identity_and_preserves_roles(
    gemini_capture: dict[str, Any],
) -> None:
    from campaign_copilot.llm.client import GeminiClient

    client = GeminiClient(model="gemini-x", project="project-x", location="global")
    response = client.complete(
        [Message(role="user", content="hi"), Message(role="assistant", content="hello")],
        system="SYS",
    )

    assert gemini_capture["init"] == {
        "enterprise": True,
        "project": "project-x",
        "location": "global",
    }
    assert [item["role"] for item in gemini_capture["generate"]["contents"]] == [
        "user",
        "model",
    ]
    assert gemini_capture["config"]["system_instruction"] == "SYS"
    assert response.usage.input_tokens == 13
    assert response.usage.output_tokens == 5
    assert client.count_tokens("hello") == 17


# ------------------------------------------------------------------ anthropic


class _FakeAnthropicResponse:
    def __init__(self, blocks: list[Any], stop: str = "end_turn") -> None:
        self.content = blocks
        self.stop_reason = stop
        self.usage = types.SimpleNamespace(input_tokens=11, output_tokens=7)


@pytest.fixture
def anthropic_capture() -> Iterator[dict[str, Any]]:
    captured: dict[str, Any] = {}

    def make_response(blocks: list[Any]) -> _FakeAnthropicResponse:
        return _FakeAnthropicResponse(blocks)

    class FakeMessages:
        def create(self, **kwargs: Any) -> _FakeAnthropicResponse:
            captured["create"] = kwargs
            return captured.get("response", make_response([_text_block("{}")]))

        def count_tokens(self, **kwargs: Any) -> Any:
            captured["count"] = kwargs
            return types.SimpleNamespace(input_tokens=42)

    class FakeAnthropic:
        def __init__(self, **kwargs: Any) -> None:
            captured["init"] = kwargs
            self.messages = FakeMessages()

    module = types.ModuleType("anthropic")
    module.Anthropic = FakeAnthropic  # type: ignore[attr-defined]
    captured["make_response"] = make_response
    yield from _install_and_yield("anthropic", module, captured)


def _install_and_yield(
    name: str, module: types.ModuleType, captured: dict[str, Any]
) -> Iterator[dict[str, Any]]:
    saved = sys.modules.get(name)
    sys.modules[name] = module
    try:
        yield captured
    finally:
        if saved is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = saved


def _text_block(text: str) -> Any:
    return types.SimpleNamespace(type="text", text=text)


def test_anthropic_adapter_flattens_text_blocks(anthropic_capture: dict[str, Any]) -> None:
    from campaign_copilot.llm.client import AnthropicClient

    anthropic_capture["response"] = _FakeAnthropicResponse(
        [_text_block('{"reasoning"'), _text_block(': "x"}')]
    )
    client = AnthropicClient(model="claude-x", api_key="k")
    response = client.complete([Message(role="user", content="hi")], system="SYS")

    assert response.text == '{"reasoning": "x"}'
    assert response.usage.input_tokens == 11
    assert response.stop_reason == "end_turn"


def test_anthropic_adapter_puts_system_in_its_own_field(
    anthropic_capture: dict[str, Any],
) -> None:
    """A system-role message in history must not be sent as a conversation turn."""
    from campaign_copilot.llm.client import AnthropicClient

    client = AnthropicClient(model="claude-x", api_key="k")
    client.complete(
        [Message(role="system", content="OLD"), Message(role="user", content="hi")],
        system="NEW",
    )
    sent = anthropic_capture["create"]
    assert sent["system"] == "NEW"
    assert [m["role"] for m in sent["messages"]] == ["user"], "history system turn dropped"


def test_anthropic_adapter_raises_on_only_non_text_blocks(
    anthropic_capture: dict[str, Any],
) -> None:
    """A response of only tool_use blocks would parse to '' and fail confusingly downstream."""
    from campaign_copilot.llm.client import AnthropicClient

    anthropic_capture["response"] = _FakeAnthropicResponse(
        [types.SimpleNamespace(type="tool_use", text="")]
    )
    client = AnthropicClient(model="claude-x", api_key="k")
    with pytest.raises(RuntimeError, match="non-text blocks"):
        client.complete([Message(role="user", content="hi")])


# --------------------------------------------------------------------- openai


@pytest.fixture
def openai_capture() -> Iterator[dict[str, Any]]:
    captured: dict[str, Any] = {}

    def response(content: str | None) -> Any:
        return types.SimpleNamespace(
            choices=[
                types.SimpleNamespace(
                    message=types.SimpleNamespace(content=content), finish_reason="stop"
                )
            ],
            usage=types.SimpleNamespace(prompt_tokens=3, completion_tokens=5),
        )

    class FakeCompletions:
        def create(self, **kwargs: Any) -> Any:
            captured["create"] = kwargs
            return captured.get("response", response("{}"))

    class FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            captured["init"] = kwargs
            self.chat = types.SimpleNamespace(completions=FakeCompletions())

    module = types.ModuleType("openai")
    module.OpenAI = FakeOpenAI  # type: ignore[attr-defined]
    captured["response_factory"] = response
    yield from _install_and_yield("openai", module, captured)


def test_openai_adapter_reads_the_first_choice(openai_capture: dict[str, Any]) -> None:
    from campaign_copilot.llm.client import OpenAIClient

    openai_capture["response"] = openai_capture["response_factory"]('{"action": "answer"}')
    client = OpenAIClient(model="gpt-x", api_key="k")
    response = client.complete([Message(role="user", content="hi")], system="SYS")

    assert response.text == '{"action": "answer"}'
    assert response.usage.output_tokens == 5
    assert response.stop_reason == "stop"


def test_openai_adapter_does_not_duplicate_the_system_message(
    openai_capture: dict[str, Any],
) -> None:
    """docs/AUDIT.md, R6-1. It used to send the history system turn and the system param."""
    from campaign_copilot.llm.client import OpenAIClient

    client = OpenAIClient(model="gpt-x", api_key="k")
    client.complete(
        [Message(role="system", content="OLD"), Message(role="user", content="hi")],
        system="NEW",
    )
    roles = [m["role"] for m in openai_capture["create"]["messages"]]
    assert roles == ["system", "user"], "exactly one system message, and it is the new one"


def test_openai_adapter_tolerates_a_null_content(openai_capture: dict[str, Any]) -> None:
    """OpenAI returns content=None for some finish reasons; that must become '' not a crash."""
    from campaign_copilot.llm.client import OpenAIClient

    openai_capture["response"] = openai_capture["response_factory"](None)
    client = OpenAIClient(model="gpt-x", api_key="k")
    assert client.complete([Message(role="user", content="hi")]).text == ""
