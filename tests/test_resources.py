"""Where the non-Python files live.

Five modules resolved their data paths relative to the *repository*, which is the same place as
the package only under an editable install. `pip install .` -- what both Dockerfiles do -- put
`parents[3]` inside site-packages and `SemanticLayer.load()` raised `FileNotFoundError` before
the service could answer a request (docs/AUDIT.md, R4-1).

These tests cover the resolver. The guarantee that actually matters -- that the *wheel* carries
the data -- cannot be asserted from a source checkout, because the checkout always satisfies the
fallback. CI's `package` job builds the wheel, installs it into a venv with no source tree, and
starts both service factories. That job is the test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from campaign_copilot.resources import REPO_ROOT, resource_dir, resource_path


def test_the_checkout_fallback_finds_the_canonical_files() -> None:
    assert (
        resource_path("metrics.yml", "semantic/metrics.yml")
        == REPO_ROOT / "semantic/metrics.yml"
    )
    assert resource_dir("prompts", "prompts") == REPO_ROOT / "prompts"
    assert resource_dir("datasets", "evals/datasets").is_dir()


def test_an_environment_variable_overrides_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deployment mounts a file somewhere the package cannot guess."""
    override = tmp_path / "custom.yml"
    override.write_text("metrics: []")
    monkeypatch.setenv("CC_METRICS_PATH", str(override))
    assert (
        resource_path("metrics.yml", "semantic/metrics.yml", env_var="CC_METRICS_PATH")
        == override
    )


def test_an_empty_environment_variable_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CC_METRICS_PATH", "")
    assert resource_path(
        "metrics.yml", "semantic/metrics.yml", env_var="CC_METRICS_PATH"
    ).is_file()


def test_a_missing_resource_names_every_place_it_looked() -> None:
    """An error that does not say where it looked is a bug report nobody can act on."""
    with pytest.raises(FileNotFoundError) as err:
        resource_path("nope.yml", "nowhere/nope.yml", env_var="CC_NOPE")
    message = str(err.value)
    assert "_data/nope.yml" in message
    assert "nowhere/nope.yml" in message
    assert "CC_NOPE" in message


def test_the_semantic_layer_and_prompts_load_without_arguments() -> None:
    from campaign_copilot.prompts import PromptRegistry
    from campaign_copilot.semantic import SemanticLayer

    assert SemanticLayer.load().metrics
    assert PromptRegistry.load().prompts
