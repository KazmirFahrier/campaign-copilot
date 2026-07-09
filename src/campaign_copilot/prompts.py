"""Versioned prompt registry.

Prompts are files, not string literals buried in a function. Every eval run records the
SHA of the prompt it used, so a metric change can be attributed to a prompt change. A
prompt that lives in a f-string cannot be bisected.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

__all__ = ["Prompt", "PromptRegistry"]

DEFAULT_PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts"


@dataclass(frozen=True)
class Prompt:
    """One prompt, identified by content hash."""

    name: str
    text: str

    @cached_property
    def sha(self) -> str:
        """First 12 hex chars of the SHA-256 of the prompt text."""
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:12]

    def __str__(self) -> str:
        """The prompt text itself, so a Prompt can be passed where a str is expected."""
        return self.text


@dataclass(frozen=True)
class PromptRegistry:
    """All prompts under a directory, keyed by filename stem."""

    prompts: dict[str, Prompt]

    @classmethod
    def load(cls, directory: str | Path | None = None) -> PromptRegistry:
        """Read every ``*.md`` in ``directory`` into the registry."""
        path = Path(directory) if directory is not None else DEFAULT_PROMPT_DIR
        if not path.is_dir():
            raise FileNotFoundError(f"No prompt directory at {path}")
        found = {
            f.stem: Prompt(name=f.stem, text=f.read_text(encoding="utf-8").strip())
            for f in sorted(path.glob("*.md"))
        }
        if not found:
            raise FileNotFoundError(f"No prompts found in {path}")
        return cls(prompts=found)

    def get(self, name: str) -> Prompt:
        """Look up a prompt by stem."""
        try:
            return self.prompts[name]
        except KeyError:
            raise KeyError(f"Unknown prompt {name!r}. Known: {sorted(self.prompts)}") from None

    def fingerprint(self) -> str:
        """A single hash over every prompt, recorded alongside each eval run."""
        joined = "".join(f"{n}:{p.sha}" for n, p in sorted(self.prompts.items()))
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]
