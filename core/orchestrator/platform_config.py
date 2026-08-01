"""The engine's one user-facing config file, and the presets it selects.

Six separate files (agents/routing/quota/context/workflow/token_budget) used
to have to be read together to predict what a run would do — real, calibrated
policy, but exposed as if a phone-driven personal gateway meant "administer a
configurable multi-agent scheduler." It doesn't: it means "fix this, test it,
show me a preview."

Two tiers now. `config/platform.yaml` is the only engine file meant for
hand-editing — five knobs. Everything else (per-role provider/model/effort
tables, the DAG shape, retrieval thresholds) is a named preset shipped inside
the engine under `config/presets/`, versioned with the code rather than
user-tuned. `.ai-platform.yml` is untouched by any of this — it is the target
repo's own policy, a different axis entirely (see target_config.py).

Mirrors that module's pattern: one frozen dataclass, a pure `_parse`, one
`load()` entry point. Loaded once per run in `supervisor.run()` and threaded
through — the same "one config snapshot per run" reasoning target_config.py
already established, so a preset can't mean two different things to two
calls in the same run, and an unknown-preset typo surfaces once, early,
instead of five calls deep into whichever module happened to read it first.

Every function elsewhere that used to self-load its own fixed-path file now
takes an optional `platform_config: PlatformConfig | None = None`, defaulting
to `load(engine_root)` when absent. That keeps standalone callers (the
`route`/`context`/`quota` CLI commands, most existing tests) working without
constructing one by hand, while `supervisor.run()` builds one instance and
passes it everywhere so a single run stays internally consistent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from core.errors import ConfigError
from core.orchestrator.router import Thresholds
from core.telemetry.quota import Budget

PLATFORM_CONFIG_PATH = Path("config/platform.yaml")

PROFILE_PRESETS_DIR = Path("config/presets/profiles")
WORKFLOW_PRESETS_DIR = Path("config/presets/workflow")
CONTEXT_PRESETS_DIR = Path("config/presets/context")

DEFAULT_PROFILE = "balanced"
DEFAULT_WORKFLOW_MODE = "standard"
DEFAULT_CONTEXT_MODE = "smart"

DEFAULT_MAX_PARALLEL = 2
DEFAULT_DECOMPOSE = True
DEFAULT_MAX_CORRECTION_ATTEMPTS = 1
DEFAULT_QUOTA_WINDOW_HOURS = 5.0


@dataclass(frozen=True)
class PlatformConfig:
    profile: str = DEFAULT_PROFILE
    quotas: dict[str, Budget] = field(default_factory=dict)
    routing: Thresholds = field(default_factory=Thresholds)
    workflow_mode: str = DEFAULT_WORKFLOW_MODE
    max_parallel: int = DEFAULT_MAX_PARALLEL
    decompose: bool = DEFAULT_DECOMPOSE
    max_correction_attempts: int = DEFAULT_MAX_CORRECTION_ATTEMPTS
    context_mode: str = DEFAULT_CONTEXT_MODE
    context_advanced: dict = field(default_factory=dict)
    """Overrides merged onto the resolved context preset's fields — the
    escape hatch for a project that needs different relevance floors without
    editing a shipped preset file."""


def _known_presets(engine_root: Path, preset_dir: Path) -> list[str]:
    """What's actually on disk, not a hardcoded allowlist — adding a preset
    file is a data change, not a code change."""
    directory = engine_root / preset_dir
    if not directory.is_dir():
        return []
    return sorted(p.stem for p in directory.glob("*.yaml"))


def _resolve_preset(engine_root: Path, preset_dir: Path, name: str, *, kind: str) -> Path:
    known = _known_presets(engine_root, preset_dir)
    if name not in known:
        available = ", ".join(known) or "(none shipped)"
        raise ConfigError(f"Unknown {kind} preset {name!r}. Available: {available}")
    return engine_root / preset_dir / f"{name}.yaml"


def profile_preset_path(engine_root: Path, profile: str) -> Path:
    return _resolve_preset(engine_root, PROFILE_PRESETS_DIR, profile, kind="profile")


def workflow_preset_path(engine_root: Path, mode: str) -> Path:
    return _resolve_preset(engine_root, WORKFLOW_PRESETS_DIR, mode, kind="workflow")


def context_preset_path(engine_root: Path, mode: str) -> Path:
    return _resolve_preset(engine_root, CONTEXT_PRESETS_DIR, mode, kind="context")


def _parse_max_parallel(data: dict) -> int:
    value = data.get("max_parallel", DEFAULT_MAX_PARALLEL)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ConfigError(f"'workflow.max_parallel' must be a positive integer, got: {value!r}")
    return value


def _parse_decompose(data: dict) -> bool:
    value = data.get("decompose", DEFAULT_DECOMPOSE)
    if not isinstance(value, bool):
        raise ConfigError(f"'workflow.decompose' must be a boolean, got: {value!r}")
    return value


def _parse_max_correction_attempts(data: dict) -> int:
    value = data.get("max_correction_attempts", DEFAULT_MAX_CORRECTION_ATTEMPTS)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ConfigError(
            f"'workflow.max_correction_attempts' must be a non-negative integer, got: {value!r}"
        )
    return value


def _parse_quotas(data: dict) -> dict[str, Budget]:
    """Declared budgets per provider. Missing/malformed entries are skipped,
    not fatal: pressure is then reported as consumption without a percentage,
    which beats refusing to run over a config gap (see core.telemetry.quota)."""
    providers = (data.get("providers") or {}).get("quotas") or {}
    if not isinstance(providers, dict):
        return {}
    budgets: dict[str, Budget] = {}
    for name, raw in providers.items():
        if not isinstance(raw, dict):
            continue
        tokens = raw.get("tokens")
        if not isinstance(tokens, int) or tokens <= 0:
            continue
        window = raw.get("window_hours", DEFAULT_QUOTA_WINDOW_HOURS)
        budgets[name] = Budget(window_hours=float(window), tokens=tokens)
    return budgets


def _parse_routing(data: dict) -> Thresholds:
    routing = data.get("routing") or {}
    if not isinstance(routing, dict):
        return Thresholds()
    return Thresholds(
        **{k: v for k, v in routing.items() if k in Thresholds.__dataclass_fields__}
    )


def _parse_advanced_context(data: dict) -> dict:
    advanced = (data.get("advanced") or {}).get("context") or {}
    if not isinstance(advanced, dict):
        raise ConfigError("'advanced.context' must be a mapping")
    return dict(advanced)


def load(engine_root: Path) -> PlatformConfig:
    """The resolved platform policy. Missing `platform.yaml` reproduces
    today's shipped defaults exactly — `balanced`/`standard`/`smart` and the
    quota/routing numbers that used to ship as separate files."""
    path = engine_root / PLATFORM_CONFIG_PATH
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None
    data = data or {}

    profile = data.get("profile", DEFAULT_PROFILE)
    if not isinstance(profile, str) or not profile.strip():
        raise ConfigError(f"'profile' must be a non-empty string, got: {profile!r}")
    profile_preset_path(engine_root, profile)  # validates it exists, raises if not

    workflow = data.get("workflow") or {}
    workflow_mode = workflow.get("mode", DEFAULT_WORKFLOW_MODE)
    if not isinstance(workflow_mode, str) or not workflow_mode.strip():
        raise ConfigError(f"'workflow.mode' must be a non-empty string, got: {workflow_mode!r}")
    workflow_preset_path(engine_root, workflow_mode)

    context = data.get("context") or {}
    context_mode = context.get("mode", DEFAULT_CONTEXT_MODE)
    if not isinstance(context_mode, str) or not context_mode.strip():
        raise ConfigError(f"'context.mode' must be a non-empty string, got: {context_mode!r}")
    context_preset_path(engine_root, context_mode)

    return PlatformConfig(
        profile=profile,
        quotas=_parse_quotas(data),
        routing=_parse_routing(data),
        workflow_mode=workflow_mode,
        max_parallel=_parse_max_parallel(workflow),
        decompose=_parse_decompose(workflow),
        max_correction_attempts=_parse_max_correction_attempts(workflow),
        context_mode=context_mode,
        context_advanced=_parse_advanced_context(data),
    )
