"""Stateful Python execution for the agent.

READ THIS BEFORE TRUSTING IT
----------------------------

This is a **resource limiter and a blast-radius reducer. It is not a security boundary.**

The code runs as the same UID, on the same kernel, with the same filesystem, as the agent
process. `os.setrlimit` caps memory and CPU. The runner neutralizes `socket` and
`subprocess` by rebinding names, which stops the accidental `pip install` and the careless
`requests.get`, and stops a *deliberate* attacker for about ninety seconds. Anyone who can
inject code here can read the process's environment, and therefore its API keys.

The production deployment (Phase 6) runs this inside a container with no network namespace,
a read-only rootfs, a dropped capability set, and a distinct UID. Everything in this module
is what remains useful *inside* that container: it turns a runaway `while True` from an
outage into a 10-second tool error. It is defence in depth, and it is the shallow layer.

`docs/threat-model.md` states this in the terms an interviewer will ask about.

Design notes
------------

State persists across turns by pickling the namespace between calls. The alternative --
replaying every prior cell -- is O(n^2) and re-runs side effects. Pickling loses anything
unpicklable (open file handles, generators, lambdas); those names are dropped and the
agent is told which, rather than silently losing them.
"""

from __future__ import annotations

import os
import pickle
import resource
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from campaign_copilot.tools.base import ToolResult, ToolSpec

__all__ = ["PythonSandbox", "SandboxConfig"]

_RUNNER = Path(__file__).with_name("_runner.py")


@dataclass(frozen=True, slots=True)
class SandboxConfig:
    """Limits applied to every execution."""

    timeout_seconds: int = 10
    memory_mb: int = 768
    cpu_seconds: int = 10
    max_output_bytes: int = 64 * 1024
    max_file_bytes: int = 8 * 1024 * 1024


def _limit_factory(config: SandboxConfig) -> Callable[[], None]:
    """Build the ``preexec_fn`` that applies rlimits in the child before ``exec``."""

    def apply_limits() -> None:  # pragma: no cover - runs only in the child process
        os.setsid()  # own process group, so a timeout kills grandchildren too
        mem = config.memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_CPU, (config.cpu_seconds, config.cpu_seconds))
        resource.setrlimit(
            resource.RLIMIT_FSIZE, (config.max_file_bytes, config.max_file_bytes)
        )
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    return apply_limits


@dataclass
class PythonSandbox:
    """Executes agent-authored Python in a limited subprocess, one namespace per session."""

    config: SandboxConfig = field(default_factory=SandboxConfig)
    scratch: Path = field(default_factory=lambda: Path(tempfile.mkdtemp(prefix="cc-sandbox-")))

    spec: ClassVar[ToolSpec] = ToolSpec(
        name="python_exec",
        description=(
            "Execute Python in a persistent session. Variables defined in one call are "
            "available in the next. Use it to reshape query results, compute deltas, and "
            "produce charts. There is no network access. Print what you want to read back."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "session_id": {"type": "string", "default": "default"},
            },
            "required": ["code"],
        },
    )

    # ------------------------------------------------------------------ internals

    def _namespace_path(self, session_id: str) -> Path:
        safe = "".join(c for c in session_id if c.isalnum() or c in "-_")[:64] or "default"
        return self.scratch / f"ns-{safe}.pkl"

    def _load_namespace(self, session_id: str) -> dict[str, Any]:
        path = self._namespace_path(session_id)
        if not path.exists():
            return {}
        with path.open("rb") as handle:
            loaded: dict[str, Any] = pickle.load(handle)
        return loaded

    def _save_namespace(self, session_id: str, namespace: dict[str, Any]) -> None:
        with self._namespace_path(session_id).open("wb") as handle:
            pickle.dump(namespace, handle)

    def reset(self, session_id: str = "default") -> None:
        """Drop a session's namespace."""
        self._namespace_path(session_id).unlink(missing_ok=True)

    # ---------------------------------------------------------------------- run

    def run(self, **kwargs: Any) -> ToolResult:
        """Execute ``code``, returning stdout and the names that survived."""
        code: str = kwargs.get("code", "")
        session_id: str = kwargs.get("session_id", "default")
        if not code.strip():
            return ToolResult.failure("EMPTY_CODE", "No code was provided.")

        work = Path(tempfile.mkdtemp(dir=self.scratch))
        payload_path, result_path = work / "in.pkl", work / "out.pkl"
        with payload_path.open("wb") as handle:
            pickle.dump(
                {"code": code, "namespace": self._load_namespace(session_id)},
                handle,
            )

        try:
            completed = subprocess.run(
                [sys.executable, "-I", str(_RUNNER), str(payload_path), str(result_path)],
                cwd=work,
                env={"PATH": "/usr/bin:/bin", "HOME": str(work)},
                capture_output=True,
                timeout=self.config.timeout_seconds,
                preexec_fn=_limit_factory(self.config),
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ToolResult.failure(
                "TIMEOUT",
                f"Execution exceeded {self.config.timeout_seconds}s and was killed. "
                "Reduce the work, or push the aggregation into SQL where it belongs.",
            )

        if not result_path.exists():
            stderr = completed.stderr.decode("utf-8", "replace")[-2000:]
            code_name = "MEMORY_OR_CRASH" if completed.returncode < 0 else "RUNTIME_ERROR"
            return ToolResult.failure(
                code_name,
                stderr or f"Interpreter exited with status {completed.returncode}.",
            )

        with result_path.open("rb") as handle:
            outcome: dict[str, Any] = pickle.load(handle)

        if outcome["error"]:
            return ToolResult.failure("EXECUTION_ERROR", outcome["error"])

        self._save_namespace(session_id, outcome["namespace"])

        stdout = outcome["stdout"][: self.config.max_output_bytes]
        truncated = len(outcome["stdout"]) > self.config.max_output_bytes
        content = stdout or "(no output; nothing was printed)"
        if truncated:
            content += f"\n\n(output truncated at {self.config.max_output_bytes} bytes)"
        if outcome["dropped"]:
            content += (
                "\n\nNOTE: these names could not be carried into the next call and were "
                f"dropped: {', '.join(sorted(outcome['dropped']))}"
            )
        return ToolResult.success(
            content,
            stdout=stdout,
            variables=sorted(outcome["namespace"]),
            dropped=sorted(outcome["dropped"]),
            truncated=truncated,
        )
