"""Provider-agnostic LLM plumbing: clients, token budgets, structured outputs."""

from campaign_copilot.llm.client import (
    AnthropicClient,
    LLMClient,
    LLMResponse,
    Message,
    OpenAIClient,
    Role,
    ScriptedClient,
    ScriptExhaustedError,
    Usage,
)
from campaign_copilot.llm.structured import (
    RepairStats,
    StructuredGenerator,
    StructuredOutputError,
)
from campaign_copilot.llm.tokens import (
    ContextBudget,
    ContextOverflowError,
    FitResult,
    HeuristicCounter,
    TokenCounter,
)

__all__ = [
    "AnthropicClient",
    "ContextBudget",
    "ContextOverflowError",
    "FitResult",
    "HeuristicCounter",
    "LLMClient",
    "LLMResponse",
    "Message",
    "OpenAIClient",
    "RepairStats",
    "Role",
    "ScriptExhaustedError",
    "ScriptedClient",
    "StructuredGenerator",
    "StructuredOutputError",
    "TokenCounter",
    "Usage",
]
