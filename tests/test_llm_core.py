"""Phase 1 tests: the LLM core, exercised entirely offline via ScriptedClient."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from campaign_copilot.llm import (
    ContextBudget,
    ContextOverflowError,
    HeuristicCounter,
    Message,
    ScriptedClient,
    ScriptExhaustedError,
    StructuredGenerator,
    StructuredOutputError,
    Usage,
)
from campaign_copilot.memory import ConversationMemory
from campaign_copilot.prompts import PromptRegistry


class Plan(BaseModel):
    """A toy schema standing in for the agent's planning output."""

    metric: str
    dimension: str
    needs_clarification: bool = False
    confidence: float = Field(ge=0.0, le=1.0)


def _user(text: str) -> Message:
    return Message(role="user", content=text)


# ------------------------------------------------------------------- structured


def test_valid_json_is_returned_on_the_first_attempt() -> None:
    client = ScriptedClient(['{"metric": "roas", "dimension": "channel", "confidence": 0.9}'])
    plan, stats = StructuredGenerator(client).generate(Plan, [_user("roas by channel")])
    assert plan.metric == "roas"
    assert stats.attempts == 1
    assert stats.valid_first_try
    assert stats.repairs == 0


def test_markdown_fences_are_stripped() -> None:
    client = ScriptedClient(
        ['```json\n{"metric": "ctr", "dimension": "channel", "confidence": 0.5}\n```']
    )
    plan, stats = StructuredGenerator(client).generate(Plan, [_user("ctr")])
    assert plan.metric == "ctr"
    assert stats.valid_first_try


def test_a_validation_failure_is_repaired_and_counted() -> None:
    """The second attempt sees the *error*, not just the same prompt again."""
    client = ScriptedClient(
        [
            '{"metric": "roas", "dimension": "channel", "confidence": 7.5}',  # out of range
            '{"metric": "roas", "dimension": "channel", "confidence": 0.75}',
        ]
    )
    plan, stats = StructuredGenerator(client).generate(Plan, [_user("roas")])
    assert plan.confidence == 0.75
    assert stats.attempts == 2
    assert stats.repairs == 1
    assert not stats.valid_first_try

    repair_turn = client.calls[-1][-1]
    assert repair_turn.role == "user"
    assert "less_than_equal" in repair_turn.content or "confidence" in repair_turn.content


def test_unparseable_json_is_repaired() -> None:
    client = ScriptedClient(
        [
            "I think the answer is roas.",
            '{"metric": "roas", "dimension": "channel", "confidence": 0.5}',
        ]
    )
    _, stats = StructuredGenerator(client).generate(Plan, [_user("roas")])
    assert stats.attempts == 2
    assert "not valid JSON" in stats.errors[0]


def test_the_repair_loop_is_bounded() -> None:
    """An unbounded repair loop is a cost incident. Three attempts, then stop."""
    client = ScriptedClient(["nope"] * 3)
    with pytest.raises(StructuredOutputError) as err:
        StructuredGenerator(client, max_repairs=2).generate(Plan, [_user("roas")])
    assert err.value.attempts == 3
    assert client.call_count == 3


def test_scripted_client_fails_loudly_when_over_called() -> None:
    client = ScriptedClient(["nope"])
    with pytest.raises(ScriptExhaustedError):
        StructuredGenerator(client, max_repairs=2).generate(Plan, [_user("roas")])


def test_usage_accumulates_across_repairs() -> None:
    client = ScriptedClient(["bad", '{"metric": "a", "dimension": "b", "confidence": 0.1}'])
    _, stats = StructuredGenerator(client).generate(Plan, [_user("q")])
    assert stats.usage.total_tokens > 0
    assert stats.usage == Usage(stats.usage.input_tokens, stats.usage.output_tokens)


# ----------------------------------------------------------------------- tokens


def test_the_heuristic_counter_over_estimates_rather_than_under() -> None:
    """A budget that passes offline must pass at the provider. Erring low is the bug."""
    counter = HeuristicCounter()
    prose = "the quick brown fox jumps over the lazy dog " * 10
    assert counter.count(prose) > len(prose) / 4


def test_budget_rejects_impossible_configuration() -> None:
    with pytest.raises(ValueError, match="max_output_tokens"):
        ContextBudget(context_window=100, max_output_tokens=100)


def test_fixed_costs_reduce_the_history_budget() -> None:
    budget = ContextBudget(context_window=10_000, max_output_tokens=1_000)
    bare = budget.available_for_history()
    with_schema = budget.available_for_history("x" * 3_200)
    assert with_schema < bare


def test_fit_evicts_oldest_first_and_keeps_recency() -> None:
    budget = ContextBudget(context_window=200, max_output_tokens=50, reserve_fraction=0.0)
    history = [_user(f"turn {i} " + "x" * 40) for i in range(10)]
    result = budget.fit(history)
    assert result.evicted_any
    assert result.kept[-1] is history[-1], "the newest turn must survive"
    assert result.evicted[0] is history[0], "the oldest turn must go first"
    assert result.tokens_used <= result.tokens_available


def test_must_keep_messages_are_never_evicted() -> None:
    budget = ContextBudget(context_window=200, max_output_tokens=50, reserve_fraction=0.0)
    history = [_user("old " + "x" * 60) for _ in range(6)]
    question = _user("what is roas by channel")
    result = budget.fit([*history, question], must_keep=[question])
    assert question in result.kept
    assert question not in result.evicted


def test_overflowing_non_evictable_content_raises_rather_than_truncating() -> None:
    budget = ContextBudget(context_window=120, max_output_tokens=50, reserve_fraction=0.0)
    huge = _user("x" * 5_000)
    with pytest.raises(ContextOverflowError, match="Reduce the system prompt"):
        budget.fit([huge], must_keep=[huge])


# ----------------------------------------------------------------------- memory


def _memory(**kw: object) -> ConversationMemory:
    budget = ContextBudget(context_window=4_000, max_output_tokens=500)
    return ConversationMemory(session_id="s1", budget=budget, **kw)  # type: ignore[arg-type]


def test_pinned_facts_appear_in_the_system_prompt() -> None:
    mem = _memory()
    mem.pin("date_range", "last 28 days")
    mem.pin("exclude_branded", "true")
    mem.add(_user("and now by channel?"))
    system, _ = mem.render("You are an analyst.")
    assert "date_range: last 28 days" in system
    assert "exclude_branded: true" in system


def test_pinned_facts_survive_compression() -> None:
    """The failure this prevents: a summary quietly drops 'exclude branded search'."""
    mem = _memory(verbatim_turns=2)
    mem.pin("exclude_branded", "true")
    for i in range(8):
        mem.add(_user(f"turn {i}"))
    folded = mem.compress(lambda msgs: f"user asked {len(msgs)} things")
    assert folded == 6
    assert mem.pinned["exclude_branded"] == "true"
    system, _ = mem.render("sys")
    assert "exclude_branded" in system


def test_compression_folds_old_turns_and_keeps_the_verbatim_window() -> None:
    mem = _memory(verbatim_turns=3)
    for i in range(10):
        mem.add(_user(f"turn {i}"))
    mem.compress(lambda msgs: "summary text")
    assert len(mem.turns) == 3
    assert mem.turns[-1].content == "turn 9"
    assert mem.summary == "summary text"


def test_compression_is_a_noop_when_the_buffer_is_short() -> None:
    mem = _memory(verbatim_turns=6)
    mem.add(_user("only one"))
    assert mem.compress(lambda _: "never called") == 0
    assert mem.summary == ""


def test_repeated_compression_folds_the_previous_summary_back_in() -> None:
    mem = _memory(verbatim_turns=2)
    for i in range(6):
        mem.add(_user(f"a{i}"))
    mem.compress(lambda msgs: "first summary")
    for i in range(6):
        mem.add(_user(f"b{i}"))
    seen: list[int] = []
    mem.compress(lambda msgs: (seen.append(len(msgs)), "second summary")[1])
    assert mem.summary == "second summary"
    assert seen[0] > 1, "the previous summary must be an input to the next one"


def test_the_final_question_and_the_summary_are_non_evictable() -> None:
    budget = ContextBudget(context_window=900, max_output_tokens=100, reserve_fraction=0.0)
    mem = ConversationMemory(session_id="s1", budget=budget, verbatim_turns=20)
    mem.summary = "earlier: the user asked about Q4"
    for i in range(60):
        mem.add(_user(f"filler turn {i} " + "x" * 80))
    mem.add(_user("so what is the answer"))
    _, result = mem.render("sys")
    assert result.evicted_any
    assert result.kept[-1].content == "so what is the answer"
    assert any("earlier: the user asked" in m.content for m in result.kept)


def test_unpin_is_idempotent() -> None:
    mem = _memory()
    mem.pin("k", "v")
    mem.unpin("k")
    mem.unpin("k")
    assert mem.pinned_block() == ""


# ---------------------------------------------------------------------- prompts


def test_prompts_load_and_hash() -> None:
    registry = PromptRegistry.load()
    prompt = registry.get("sql_analyst")
    assert "semantic layer" in prompt.text
    assert len(prompt.sha) == 12


def test_prompt_hash_changes_with_content() -> None:
    from campaign_copilot.prompts import Prompt

    assert Prompt("p", "a").sha != Prompt("p", "b").sha


def test_unknown_prompt_lists_the_known_ones() -> None:
    registry = PromptRegistry.load()
    with pytest.raises(KeyError, match="sql_analyst"):
        registry.get("does_not_exist")


def test_registry_fingerprint_is_stable() -> None:
    assert PromptRegistry.load().fingerprint() == PromptRegistry.load().fingerprint()


def test_identical_turns_are_tracked_by_identity_not_equality() -> None:
    """Two textually identical turns must not share an eviction decision."""
    # Sized so exactly one of the two messages fits alongside the protected one.
    budget = ContextBudget(context_window=110, max_output_tokens=50, reserve_fraction=0.0)
    first = _user("same text " + "x" * 120)
    second = _user("same text " + "x" * 120)
    assert first == second
    result = budget.fit([first, second], must_keep=[second])
    assert any(m is second for m in result.kept)
    assert any(m is first for m in result.evicted)
