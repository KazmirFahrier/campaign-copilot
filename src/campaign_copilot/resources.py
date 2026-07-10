"""Where the non-Python files live.

Five modules computed their data paths as `Path(__file__).parents[3] / "semantic" / ...`,
which is the repository root *only when the package is imported from a source checkout*. Under
`pip install .` -- which is exactly what both Dockerfiles do -- `parents[3]` is somewhere
inside
`site-packages`, and `SemanticLayer.load()` raises `FileNotFoundError` before the service can
answer its first request (docs/AUDIT.md, R4-1).

Nothing caught this because CI installs with `-e`, the test suite runs from the checkout, and
the images were never built. An editable install makes a packaged application look like a
script, and the difference only shows up in the one environment nobody exercised.

Resolution order, most specific first:

1. **An environment variable.** Deployment overrides everything: `CC_METRICS_PATH` points at a
   mounted file, `CC_PROMPTS_DIR` at a mounted directory.
2. **Data shipped inside the wheel** (``campaign_copilot/_data/``). `pyproject.toml`
   force-includes the canonical files there at build time, so the wheel is self-contained and
   the repository keeps exactly one copy of each under version control.
3. **The source checkout.** Development, tests, `make eval`.

If none of the three exists, raise with all three locations named. A `FileNotFoundError` that
does not say where it looked is a bug report nobody can act on.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["REPO_ROOT", "resource_dir", "resource_path"]

#: `src/campaign_copilot/resources.py` -> `src/campaign_copilot` -> `src` -> repo root.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Populated by `[tool.hatch.build.targets.wheel.force-include]` when the wheel is built.
_PACKAGED = Path(__file__).resolve().parent / "_data"


def _candidates(packaged: str, repo_relative: str, env_var: str | None) -> list[Path]:
    found: list[Path] = []
    if env_var and (override := os.getenv(env_var)):
        found.append(Path(override))
    found.append(_PACKAGED / packaged)
    found.append(REPO_ROOT / repo_relative)
    return found


def _resolve(packaged: str, repo_relative: str, env_var: str | None, *, want_dir: bool) -> Path:
    tried = _candidates(packaged, repo_relative, env_var)
    for candidate in tried:
        if candidate.is_dir() if want_dir else candidate.is_file():
            return candidate
    kind = "directory" if want_dir else "file"
    locations = "\n  ".join(str(p) for p in tried)
    hint = f" Set {env_var} to override." if env_var else ""
    raise FileNotFoundError(
        f"Could not find the {kind} {repo_relative!r}. Looked in:\n  {locations}\n"
        f"If this package was installed rather than run from a checkout, the wheel is missing "
        f"its data files.{hint}"
    )


def resource_path(packaged: str, repo_relative: str, env_var: str | None = None) -> Path:
    """Locate a data *file* shipped with the package."""
    return _resolve(packaged, repo_relative, env_var, want_dir=False)


def resource_dir(packaged: str, repo_relative: str, env_var: str | None = None) -> Path:
    """Locate a data *directory* shipped with the package."""
    return _resolve(packaged, repo_relative, env_var, want_dir=True)
