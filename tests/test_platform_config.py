"""Tests for core.orchestrator.platform_config — the engine's one user-facing
config file plus the internal presets it selects.

`load()` is the single entry point that used to be six separate files' worth
of self-loading; these tests cover the same properties `target_config.py`'s
tests cover for the same reason — a missing file reproduces today's shipped
defaults, an unknown preset name fails loudly with what's actually available,
and every scalar the old six files carried still round-trips.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.errors import ConfigError
from core.orchestrator import platform_config as pc
from core.orchestrator.router import Thresholds
from core.telemetry.quota import Budget


def _presets(engine_root: Path) -> None:
    """The minimum preset tree load() needs to resolve its defaults."""
    (engine_root / "config/presets/profiles").mkdir(parents=True)
    (engine_root / "config/presets/profiles/balanced.yaml").write_text(
        "backend:\n  profiles:\n    - {provider: claude_code}\n", encoding="utf-8"
    )
    (engine_root / "config/presets/profiles/max.yaml").write_text(
        "backend:\n  profiles:\n    - {provider: codex_cli}\n", encoding="utf-8"
    )
    (engine_root / "config/presets/workflow").mkdir(parents=True)
    (engine_root / "config/presets/workflow/standard.yaml").write_text(
        "tasks:\n  - {id: backend, agent: backend, depends_on: []}\n", encoding="utf-8"
    )
    (engine_root / "config/presets/context").mkdir(parents=True)
    for mode in ("smart", "full", "minimal"):
        (engine_root / f"config/presets/context/{mode}.yaml").write_text(
            "use_git_diff: true\n", encoding="utf-8"
        )


@pytest.fixture
def engine_root(tmp_path: Path) -> Path:
    _presets(tmp_path)
    return tmp_path


def _write_platform(engine_root: Path, body: str) -> None:
    (engine_root / "config").mkdir(exist_ok=True)
    (engine_root / pc.PLATFORM_CONFIG_PATH).write_text(body, encoding="utf-8")


# --- missing file reproduces today's shipped defaults ---


def test_missing_platform_yaml_uses_defaults_matching_the_old_six_files(engine_root: Path) -> None:
    config = pc.load(engine_root)

    assert config.profile == "balanced"
    assert config.workflow_mode == "standard"
    assert config.context_mode == "smart"
    assert config.max_parallel == 2
    assert config.decompose is True
    assert config.max_correction_attempts == 1
    assert config.quotas == {}
    assert config.routing == Thresholds()
    assert config.context_advanced == {}


# --- every scalar the old six files carried round-trips ---


def test_profile_is_read(engine_root: Path) -> None:
    _write_platform(engine_root, "profile: max\n")
    assert pc.load(engine_root).profile == "max"


def test_workflow_scalars_are_read(engine_root: Path) -> None:
    _write_platform(
        engine_root,
        "workflow:\n  mode: standard\n  max_parallel: 4\n  decompose: false\n"
        "  max_correction_attempts: 3\n",
    )
    config = pc.load(engine_root)
    assert config.max_parallel == 4
    assert config.decompose is False
    assert config.max_correction_attempts == 3


def test_context_mode_is_read(engine_root: Path) -> None:
    _write_platform(engine_root, "context:\n  mode: minimal\n")
    assert pc.load(engine_root).context_mode == "minimal"


def test_routing_thresholds_are_read(engine_root: Path) -> None:
    _write_platform(
        engine_root,
        "routing:\n  max_quota_ratio: 0.5\n  min_success_rate: 0.9\n"
        "  min_samples: 10\n  window_hours: 48\n",
    )
    assert pc.load(engine_root).routing == Thresholds(
        max_quota_ratio=0.5, min_success_rate=0.9, min_samples=10, window_hours=48
    )


def test_quotas_are_read(engine_root: Path) -> None:
    _write_platform(
        engine_root,
        "providers:\n  quotas:\n    codex_cli: {tokens: 8000000, window_hours: 5}\n",
    )
    assert pc.load(engine_root).quotas == {"codex_cli": Budget(window_hours=5.0, tokens=8000000)}


def test_malformed_quota_entries_are_skipped_not_fatal(engine_root: Path) -> None:
    """Missing/malformed entries are skipped, not fatal — pressure is then
    reported as consumption without a percentage rather than the run
    refusing to start over a config gap."""
    _write_platform(
        engine_root,
        "providers:\n  quotas:\n"
        "    codex_cli: {tokens: 8000000, window_hours: 5}\n"
        "    claude_code: not_a_mapping\n"
        "    openai_api: {window_hours: 5}\n",  # no tokens
    )
    assert set(pc.load(engine_root).quotas) == {"codex_cli"}


def test_advanced_context_overrides_are_read(engine_root: Path) -> None:
    _write_platform(
        engine_root,
        "advanced:\n  context:\n    min_similarity: 0.5\n    max_files: 3\n",
    )
    assert pc.load(engine_root).context_advanced == {"min_similarity": 0.5, "max_files": 3}


# --- unknown preset names fail loudly, before any worktree or branch exists ---


def test_unknown_profile_lists_what_is_actually_available(engine_root: Path) -> None:
    _write_platform(engine_root, "profile: nonexistent\n")
    with pytest.raises(ConfigError, match="Unknown profile preset 'nonexistent'.*balanced, max"):
        pc.load(engine_root)


def test_unknown_workflow_mode_lists_what_is_actually_available(engine_root: Path) -> None:
    _write_platform(engine_root, "workflow:\n  mode: exotic\n")
    with pytest.raises(ConfigError, match="Unknown workflow preset 'exotic'.*standard"):
        pc.load(engine_root)


def test_unknown_context_mode_lists_what_is_actually_available(engine_root: Path) -> None:
    _write_platform(engine_root, "context:\n  mode: bogus\n")
    with pytest.raises(ConfigError, match="Unknown context preset 'bogus'"):
        pc.load(engine_root)


def test_adding_a_preset_file_needs_no_code_change(engine_root: Path) -> None:
    """Preset names are discovered from what's on disk, not a hardcoded
    Python allowlist -- confirms a new preset is a data change only."""
    (engine_root / "config/presets/profiles/cheap.yaml").write_text(
        "backend:\n  profiles:\n    - {provider: claude_code}\n", encoding="utf-8"
    )
    _write_platform(engine_root, "profile: cheap\n")
    assert pc.load(engine_root).profile == "cheap"


# --- preset path resolvers ---


def test_profile_preset_path_resolves_under_the_engine_root(engine_root: Path) -> None:
    assert pc.profile_preset_path(engine_root, "balanced") == (
        engine_root / "config/presets/profiles/balanced.yaml"
    )


def test_workflow_preset_path_resolves_under_the_engine_root(engine_root: Path) -> None:
    assert pc.workflow_preset_path(engine_root, "standard") == (
        engine_root / "config/presets/workflow/standard.yaml"
    )


def test_context_preset_path_resolves_under_the_engine_root(engine_root: Path) -> None:
    assert pc.context_preset_path(engine_root, "smart") == (
        engine_root / "config/presets/context/smart.yaml"
    )


# --- scalar validation (moved here from planner.py's now-deleted parsing) ---


def test_max_parallel_must_be_a_positive_integer(engine_root: Path) -> None:
    _write_platform(engine_root, "workflow:\n  max_parallel: 0\n")
    with pytest.raises(ConfigError, match="max_parallel"):
        pc.load(engine_root)


def test_decompose_must_be_a_boolean(engine_root: Path) -> None:
    _write_platform(engine_root, "workflow:\n  decompose: yes_please\n")
    with pytest.raises(ConfigError, match="decompose"):
        pc.load(engine_root)


def test_max_correction_attempts_must_be_non_negative(engine_root: Path) -> None:
    _write_platform(engine_root, "workflow:\n  max_correction_attempts: -1\n")
    with pytest.raises(ConfigError, match="max_correction_attempts"):
        pc.load(engine_root)
