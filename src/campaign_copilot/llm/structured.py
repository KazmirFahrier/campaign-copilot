"""Structured outputs, with a bounded repair loop.

Every response the agent acts on is a validated Pydantic model, never a string it hopes
is JSON. When validation fails the *specific* error is fed back to the model, which is
the difference between a repair and a retry: a retry re-rolls the dice, a repair tells
the model what was wrong.

The loop is bounded at ``max_repairs``. An unbounded repair loop is a cost incident.

``RepairStats`` is not decoration. The first-attempt validity rate is a headline metric
in the Phase 4 eval report (target: >= 0.97), and it is the fastest signal that a prompt
edit has regressed. It only exists because it is recorded here, per call.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from campaign_copilot.llm.client import LLMClient, Message, Usage

__all__ = ["RepairStats", "StructuredGenerator", "StructuredOutputError"]

T = TypeVar("T", bound=BaseModel)

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)

_INSTRUCTION = (
    "Respond with a single JSON object matching this schema. Output nothing else: no "
    "prose, no explanation, no markdown fences.\n\nSchema:\n{schema}"
)

_REPAIR = (
    "Your previous response did not validate against the schema.\n\n"
    "Error:\n{error}\n\n"
    "Return the corrected JSON object only. Do not apologise or explain."
)


class StructuredOutputError(RuntimeError):
    """The model could not produce schema-valid output within the repair budget."""

    def __init__(self, attempts: int, last_error: str) -> None:
        """Record how many attempts were spent and why the last one failed."""
        super().__init__(
            f"No schema-valid response after {attempts} attempt(s). Last error: {last_error}"
        )
        self.attempts = attempts
        self.last_error = last_error


@dataclass(slots=True)
class RepairStats:
    """Telemetry for one :meth:`StructuredGenerator.generate` call."""

    attempts: int = 0
    usage: Usage = field(default_factory=Usage)
    errors: list[str] = field(default_factory=list)

    @property
    def repairs(self) -> int:
        """Attempts beyond the first."""
        return max(0, self.attempts - 1)

    @property
    def valid_first_try(self) -> bool:
        """The metric the eval harness aggregates into ``schema_validity_rate``."""
        return self.attempts == 1 and not self.errors


def _strip_fences(text: str) -> str:
    """Remove markdown code fences the model was asked not to emit but sometimes does."""
    return _FENCE.sub("", text).strip()


@dataclass(slots=True)
class StructuredGenerator:
    """Wraps an :class:`LLMClient` so every response is a validated model."""

    client: LLMClient
    max_repairs: int = 2

    def generate(
        self,
        schema: type[T],
        messages: Sequence[Message],
        *,
        system: str | None = None,
        max_tokens: int = 1024,
    ) -> tuple[T, RepairStats]:
        """Return a validated instance of ``schema`` plus the telemetry for the call.

        Raises:
            StructuredOutputError: if the repair budget is exhausted.
        """
        stats = RepairStats()
        instruction = _INSTRUCTION.format(
            schema=json.dumps(schema.model_json_schema(), indent=2)
        )
        full_system = f"{system}\n\n{instruction}" if system else instruction
        conversation = list(messages)
        last_error = "unknown"

        for _ in range(self.max_repairs + 1):
            response = self.client.complete(
                conversation, system=full_system, max_tokens=max_tokens
            )
            stats.attempts += 1
            stats.usage = stats.usage + response.usage

            try:
                payload = json.loads(_strip_fences(response.text))
            except json.JSONDecodeError as err:
                last_error = f"Response was not valid JSON: {err}"
            else:
                try:
                    return schema.model_validate(payload), stats
                except ValidationError as err:
                    last_error = err.json(indent=2)

            stats.errors.append(last_error)
            conversation = [
                *conversation,
                Message(role="assistant", content=response.text),
                Message(role="user", content=_REPAIR.format(error=last_error)),
            ]

        raise StructuredOutputError(stats.attempts, last_error)
