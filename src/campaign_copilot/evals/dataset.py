"""Eval dataset schemas.

A golden case stores the *question* and the *gold SQL*, never the gold numbers. The
expected result set is recomputed by executing the gold SQL at run time. Two reasons:

* Numbers copied into a fixture drift the moment the generator's seed or the dbt models
  change, and a stale fixture fails loudly in the wrong place.
* A gold answer written by hand is a second implementation of the metric, which can carry
  the same bug as the first. Executing the definition is the only check that means anything.

The cost is that a wrong gold SQL is a silently wrong case. `make eval-verify` executes
every one and fails on an empty result set, which catches most of that.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from campaign_copilot.resources import resource_dir

__all__ = [
    "AdversarialCase",
    "Category",
    "GoldenCase",
    "MultiTurnCase",
    "load_adversarial",
    "load_golden",
    "load_multi_turn",
]


def _dataset_dir() -> Path:
    """Resolved lazily; see campaign_copilot.resources."""
    return resource_dir("datasets", "evals/datasets")


Category = Literal["simple", "join", "window", "ambiguous"]


class GoldenCase(BaseModel):
    """One natural-language question with a verified SQL answer."""

    id: str
    question: str
    category: Category
    gold_sql: str
    gold_tools: list[str] = Field(default_factory=list)
    expects_clarification: bool = False
    notes: str = ""

    # The plan the oracle policy executes. Absent for clarification cases.
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[str] = Field(default_factory=list)
    having: list[str] = Field(default_factory=list)
    order_by: str | None = None
    limit: int | None = None


class MultiTurnCase(BaseModel):
    """A conversation. Each turn is a question; the last one carries the assertion."""

    id: str
    turns: list[str]
    tests: str
    expects_pinned: dict[str, str] = Field(default_factory=dict)
    final_gold_sql: str | None = None


class AdversarialCase(BaseModel):
    """A prompt or a document that tries to make the agent do something it must not."""

    id: str
    question: str
    vector: Literal["injection", "destructive_sql", "exfiltration", "invented_metric"]
    memo: str | None = None
    must_not_contain: list[str] = Field(default_factory=list)


def _read(path: Path) -> Iterator[dict[str, object]]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line and not line.startswith("//"):
                yield json.loads(line)


def load_golden(path: Path | None = None) -> list[GoldenCase]:
    """Load `golden_sql.jsonl`."""
    return [
        GoldenCase.model_validate(r) for r in _read(path or _dataset_dir() / "golden_sql.jsonl")
    ]


def load_multi_turn(path: Path | None = None) -> list[MultiTurnCase]:
    """Load `multi_turn.jsonl`."""
    return [
        MultiTurnCase.model_validate(r)
        for r in _read(path or _dataset_dir() / "multi_turn.jsonl")
    ]


def load_adversarial(path: Path | None = None) -> list[AdversarialCase]:
    """Load `adversarial.jsonl`."""
    return [
        AdversarialCase.model_validate(r)
        for r in _read(path or _dataset_dir() / "adversarial.jsonl")
    ]
