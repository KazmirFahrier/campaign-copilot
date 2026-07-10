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

import contextvars
import pickle
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from campaign_copilot.tools.base import ToolResult, ToolSpec

__all__ = ["PythonSandbox", "SandboxConfig", "session_scope"]

#: The session whose namespace the sandbox may touch. Set by the server, per request.
#:
#: An earlier version accepted `session_id` as a tool argument, which put it in the model's
#: action space: a persuaded agent could name another user's session and read their variables
#: (docs/AUDIT.md, P1-4). A session id is server state. The model does not get to choose one.
session_scope: contextvars.ContextVar[str] = contextvars.ContextVar(
    "session_scope", default="default"
)

_RUNNER = Path(__file__).with_name("_runner.py")


@dataclass(frozen=True, slots=True)
class SandboxConfig:
    """Limits applied to every execution."""

    timeout_seconds: int = 10
    memory_mb: int = 768
    cpu_seconds: int = 10
    max_output_bytes: int = 64 * 1024
    max_file_bytes: int = 8 * 1024 * 1024


# `preexec_fn` used to live here. It ran arbitrary Python between fork() and exec(), where
# only async-signal-safe calls are legal, and the api service invokes the agent from a worker
# thread (docs/AUDIT.md, P1-5): a lock held by another thread at fork time is held forever in
# the child. The limits are now applied by `_runner.py` in the child *after* exec, where the
# process is single-threaded and `resource.setrlimit` is ordinary Python. `os.setsid()` has a
# dedicated, safe flag: `start_new_session=True`.


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
            "properties": {"code": {"type": "string"}},
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
        # Never from kwargs: see `session_scope`.
        session_id: str = session_scope.get()
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
                env={
                    "PATH": "/usr/bin:/bin",
                    "HOME": str(work),
                    "CC_MEMORY_MB": str(self.config.memory_mb),
                    "CC_CPU_SECONDS": str(self.config.cpu_seconds),
                    "CC_MAX_FILE_BYTES": str(self.config.max_file_bytes),
                },
                capture_output=True,
                timeout=self.config.timeout_seconds,
                start_new_session=True,  # own process group; a timeout kills grandchildren
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
