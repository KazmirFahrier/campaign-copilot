"""Child process entry point for :class:`campaign_copilot.tools.python_exec.PythonSandbox`.

Runs in an isolated interpreter (``python -I``) under rlimits set by the parent's
``preexec_fn``. Never imported by the parent; invoked as a script.

The network neutralization below is best-effort and bypassable by design-aware code
(``importlib.reload(socket)`` defeats it). It exists to stop the agent from *accidentally*
calling out -- a `requests.get` in generated plotting code, a `pip install` reflex -- not
to contain an adversary. Containment is the container's job. See the module docstring of
``python_exec.py`` and ``docs/threat-model.md``.
"""

from __future__ import annotations

import builtins
import contextlib
import io
import os
import pickle
import sys
import traceback
from typing import Any


def _apply_limits() -> None:
    """Apply rlimits to this (single-threaded, post-exec) process.

    This used to run in the parent's ``preexec_fn``, between fork() and exec(), which the
    stdlib documents as unsafe in a threaded process. Here it is ordinary Python.
    """
    import resource

    mem = int(os.environ.get("CC_MEMORY_MB", "768")) * 1024 * 1024
    cpu = int(os.environ.get("CC_CPU_SECONDS", "10"))
    fsize = int(os.environ.get("CC_MAX_FILE_BYTES", str(8 * 1024 * 1024)))
    resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _deny(*_args: Any, **_kwargs: Any) -> None:
    raise PermissionError("Network and process spawning are disabled in the sandbox.")


def _neutralize() -> None:
    """Rebind the obvious escape hatches before user code runs."""
    import socket
    import subprocess

    socket.socket = _deny  # type: ignore[assignment,misc]
    socket.create_connection = _deny  # type: ignore[assignment]
    subprocess.Popen = _deny  # type: ignore[assignment,misc]
    subprocess.run = _deny  # type: ignore[assignment]
    builtins.__import__ = _guarded_import(builtins.__import__)


def _guarded_import(original: Any) -> Any:
    blocked = {"ctypes", "socketserver", "http.client", "urllib.request", "ftplib"}

    def guarded(name: str, *args: Any, **kwargs: Any) -> Any:
        if name in blocked:
            raise PermissionError(f"Importing {name!r} is disabled in the sandbox.")
        return original(name, *args, **kwargs)

    return guarded


def _is_picklable(value: Any) -> bool:
    try:
        pickle.dumps(value)
    except Exception:
        return False
    return True


def main() -> int:
    """Execute the payload's code and write the outcome to the result path."""
    payload_path, result_path = sys.argv[1], sys.argv[2]
    with open(payload_path, "rb") as handle:
        payload = pickle.load(handle)

    namespace: dict[str, Any] = dict(payload["namespace"])
    namespace.setdefault("__name__", "__cell__")

    _apply_limits()
    _neutralize()

    stdout = io.StringIO()
    error: str | None = None
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stdout):
            exec(compile(payload["code"], "<cell>", "exec"), namespace)
    except BaseException:
        error = traceback.format_exc(limit=5)

    survivors: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in namespace.items():
        if key.startswith("__"):
            continue
        if _is_picklable(value):
            survivors[key] = value
        else:
            dropped.append(key)

    with open(result_path, "wb") as handle:
        pickle.dump(
            {
                "stdout": stdout.getvalue(),
                "namespace": survivors,
                "dropped": dropped,
                "error": error,
            },
            handle,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
