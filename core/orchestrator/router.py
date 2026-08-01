"""Which provider serves a role, and why.

Retrieval proposes, an arbiter decides, every decision leaves a reason — the
same shape as core.context.selection, applied to providers instead of files.

The authority is deliberately limited. The preference order declared in the
selected profile preset (config/presets/profiles/<profile>.yaml, see
core.orchestrator.platform_config) governs; this module overrides it only on
two things it can measure — quota pressure and repeated failure on a large
enough sample. It does
not arbitrate on marginal quality, because the histories here are a handful of
calls deep and a policy inferred from that would be superstition wearing a
percentage sign.

Costs in dollars are absent on purpose. The engine drives flat-rate
subscriptions: the money is spent either way, and what actually binds is
allowance.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from core.errors import ConfigError
from core.telemetry import quota as quota_store
from core.telemetry import store as telemetry

if TYPE_CHECKING:
    # core.orchestrator.platform_config imports Thresholds from this module
    # at its own top level; importing it back here would be circular, so the
    # real import is deferred into route() itself, and this one is type-check
    # only (core.orchestrator.py's own `from __future__ import annotations`
    # already makes annotations lazy strings at runtime).
    from core.orchestrator.platform_config import PlatformConfig

PREFERRED = "preferred"
NO_HISTORY = "no_history"
OVER_QUOTA = "over_quota"
FAILING_ROLE = "failing_role"
ALL_GATED = "all_gated"
DEFAULT_COMPLEXITY = "complex"
COMPLEXITIES = {"routine", "complex", "critical"}
PROVIDER_EFFORTS = {
    "codex_cli": {"minimal", "low", "medium", "high", "xhigh"},
    # Claude Code accepts these through --effort. Ultracode is a session
    # orchestration setting (with xhigh model effort), represented here
    # because it is selected through the same CLI flag.
    "claude_code": {"low", "medium", "high", "xhigh", "max", "ultracode"},
}


@dataclass(frozen=True)
class Thresholds:
    max_quota_ratio: float = 0.85
    min_success_rate: float = 0.6
    min_samples: int = 5
    window_hours: float = 24.0


@dataclass(frozen=True)
class Candidate:
    """One provider considered for a role, and what became of it."""

    provider: str
    rank: int
    chosen: bool
    rule: str
    reason: str
    model: str | None = None
    reasoning_effort: str | None = None
    quota_ratio: float | None = None
    success_rate: float | None = None
    calls: int = 0


@dataclass(frozen=True)
class Decision:
    provider: str
    rule: str
    reason: str
    """Stored verbatim in the calls table's routing_reason column — the column
    that answers "why this provider?" and has been empty since it was created."""
    candidates: list[Candidate]
    model: str | None = None
    reasoning_effort: str | None = None
    complexity: str = DEFAULT_COMPLEXITY


@dataclass(frozen=True)
class ExecutionProfile:
    provider: str
    model: str | None = None
    reasoning_effort: str | None = None

    @property
    def label(self) -> str:
        details = "/".join(v for v in (self.model, self.reasoning_effort) if v)
        return f"{self.provider} ({details})" if details else self.provider


def _validate_complexity(complexity: str) -> None:
    if complexity not in COMPLEXITIES:
        allowed = ", ".join(sorted(COMPLEXITIES))
        raise ConfigError(f"Unsupported task complexity {complexity!r}. Available: {allowed}")


def _declared_profiles(entry: dict, agent: str, complexity: str):
    """Returns the role's list for this complexity, with a base fallback."""
    overrides = entry.get("profiles_by_complexity", {})
    if overrides is None:
        overrides = {}
    if not isinstance(overrides, dict):
        raise ConfigError(
            f"Agent role '{agent}' has invalid profiles_by_complexity: expected a mapping"
        )
    unknown = sorted(set(overrides) - COMPLEXITIES)
    if unknown:
        raise ConfigError(
            f"Agent role '{agent}' declares unknown complexity profile(s): {', '.join(unknown)}"
        )
    if complexity in overrides:
        return overrides[complexity]
    return entry.get("profiles") or entry.get("providers")


def _effort(item: dict, agent: str) -> str | None:
    if "effort" in item and "reasoning_effort" in item:
        raise ConfigError(
            f"Execution profile for agent '{agent}' declares both 'effort' and "
            "legacy 'reasoning_effort'; keep only 'effort'"
        )
    value = item.get("effort", item.get("reasoning_effort"))
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ConfigError(f"Invalid effort for agent '{agent}': {value!r}")
    return value


def eligible_profiles(
    engine_root: Path,
    agent: str,
    complexity: str = DEFAULT_COMPLEXITY,
    *,
    profile: str = "balanced",
) -> list[ExecutionProfile]:
    """The declared preference order for a role and task complexity, from the
    named `config/presets/profiles/<profile>.yaml` (see
    core.orchestrator.platform_config).

    `profiles_by_complexity` may override the base `profiles` list. Bare
    `providers: [a, b]` and legacy `provider: a` entries remain supported.
    """
    from core.orchestrator import platform_config

    _validate_complexity(complexity)
    path = platform_config.profile_preset_path(engine_root, profile)
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    if agent not in config:
        known = ", ".join(sorted(config)) or "(none configured)"
        raise ConfigError(f"Unknown agent role '{agent}'. Configured roles: {known}")

    entry = config[agent] or {}
    if not isinstance(entry, dict):
        raise ConfigError(f"Agent role '{agent}' must be a mapping, got: {entry!r}")
    declared = _declared_profiles(entry, agent, complexity)
    if not declared and entry.get("provider"):
        declared = [{
            key: entry[key]
            for key in ("provider", "model", "effort", "reasoning_effort")
            if key in entry
        }]
    if not declared:
        raise ConfigError(
            f"Agent role '{agent}' declares no providers in profile {profile!r} ({path}). "
            "Set `profiles: [{provider: name, ...}]` or `providers: [name, ...]`."
        )
    if not isinstance(declared, list):
        raise ConfigError(f"Agent role '{agent}' profiles must be a list, got: {declared!r}")

    profiles = []
    for item in declared:
        if isinstance(item, str):
            profiles.append(ExecutionProfile(item))
        elif isinstance(item, dict) and item.get("provider"):
            effort = _effort(item, agent)
            profile = ExecutionProfile(str(item["provider"]), item.get("model"), effort)
            if profile.model is not None and (
                not isinstance(profile.model, str) or not profile.model.strip()
            ):
                raise ConfigError(f"Invalid model for agent '{agent}': {profile.model!r}")
            allowed_efforts = PROVIDER_EFFORTS.get(profile.provider)
            if (
                profile.reasoning_effort is not None
                and allowed_efforts is not None
                and profile.reasoning_effort not in allowed_efforts
            ):
                allowed = ", ".join(sorted(allowed_efforts))
                raise ConfigError(
                    f"Unsupported {profile.provider} effort {profile.reasoning_effort!r} "
                    f"for agent '{agent}'. Available: {allowed}"
                )
            profiles.append(profile)
        else:
            raise ConfigError(f"Invalid execution profile for agent '{agent}': {item!r}")
    return profiles


def route(
    engine_root: Path,
    agent: str,
    known_providers: set[str],
    *,
    platform_config: "PlatformConfig | None" = None,
    complexity: str = DEFAULT_COMPLEXITY,
) -> Decision:
    """Picks a provider for this role and explains the choice.

    `engine_root` is the ai-platform install, never a target repo's worktree:
    provider eligibility, thresholds and the recorded history this reads are
    all engine-scoped, not per-project, so they stay fixed no matter which
    repo a task is currently operating on.

    `platform_config` defaults to a fresh load when not given, so a standalone
    caller (the `route`/`quota` CLI commands, most tests) needs nothing extra.
    `supervisor.run()` loads one instance and passes it to every call for a
    run, so the whole run is judged against the same snapshot of policy.

    Never returns without a provider. If every candidate is gated, the declared
    first choice runs anyway and the reason says so: a tool driven from a phone
    must not refuse to work because a config threshold was crossed. Degrading
    loudly beats failing.
    """
    from core.orchestrator import platform_config as pc

    if platform_config is None:
        platform_config = pc.load(engine_root)
    thresholds = platform_config.routing
    declared = eligible_profiles(engine_root, agent, complexity, profile=platform_config.profile)

    unknown = [p.provider for p in declared if p.provider not in known_providers]
    if unknown:
        known = ", ".join(sorted(known_providers))
        raise ConfigError(
            f"Unknown provider(s) {', '.join(unknown)} for agent '{agent}'. Available: {known}"
        )

    pressure = {
        row["provider"]: row
        for row in quota_store.pressure(
            engine_root, platform_config.quotas, window_hours=thresholds.window_hours
        )
    }
    candidates: list[Candidate] = []
    chosen: ExecutionProfile | None = None

    for rank, profile in enumerate(declared, start=1):
        provider = profile.provider
        quota_ratio = (pressure.get(provider) or {}).get("used_ratio")
        record = telemetry.role_performance(
            engine_root, agent, window_hours=thresholds.window_hours,
            provider=provider, model=profile.model, reasoning_effort=profile.reasoning_effort,
        ).get(provider) or {}
        calls = record.get("calls", 0)
        success_rate = record.get("success_rate")

        if chosen is None and quota_ratio is not None and quota_ratio > thresholds.max_quota_ratio:
            candidates.append(
                Candidate(
                    provider, rank, False, OVER_QUOTA,
                    f"{profile.label}: at {quota_ratio:.0%} of its declared budget, over the "
                    f"{thresholds.max_quota_ratio:.0%} ceiling",
                    profile.model, profile.reasoning_effort, quota_ratio, success_rate, calls,
                )
            )
            continue

        # The sample size gates the rule rather than decorating it: without
        # this, one bad call out of two retires a provider from a role.
        if (
            chosen is None
            and calls >= thresholds.min_samples
            and success_rate is not None
            and success_rate < thresholds.min_success_rate
        ):
            candidates.append(
                Candidate(
                    provider, rank, False, FAILING_ROLE,
                    f"{profile.label}: succeeded {success_rate:.0%} of {calls} times on this "
                    "role, below the "
                    f"{thresholds.min_success_rate:.0%} floor",
                    profile.model, profile.reasoning_effort, quota_ratio, success_rate, calls,
                )
            )
            continue

        if chosen is None:
            chosen = profile
            if calls == 0:
                rule, reason = NO_HISTORY, (
                    f"{profile.label}: first choice; no recorded history on this profile yet"
                )
            else:
                rule = PREFERRED
                reason = (
                    f"{profile.label}: first choice clearing both gates — "
                    f"{success_rate:.0%} success over "
                    f"{calls} calls"
                )
                if quota_ratio is not None:
                    reason += f", {quota_ratio:.0%} of budget used"
            candidates.append(
                Candidate(provider, rank, True, rule, reason, profile.model, profile.reasoning_effort, quota_ratio, success_rate, calls)
            )
        else:
            candidates.append(
                Candidate(
                    provider, rank, False, "not_needed",
                    f"not reached — {profile.label}; an earlier choice cleared the gates",
                    profile.model, profile.reasoning_effort, quota_ratio, success_rate, calls,
                )
            )

    if chosen is not None:
        picked = next(c for c in candidates if c.chosen)
        return Decision(
            chosen.provider, picked.rule, picked.reason, candidates,
            chosen.model, chosen.reasoning_effort, complexity,
        )

    # Everything was gated. Run the declared first choice regardless, and say
    # so plainly — the alternative is refusing to work, which is worse.
    fallback = declared[0]
    reason = (
        f"every candidate was gated ({'; '.join(c.reason for c in candidates)}); "
        f"running {fallback.label} anyway rather than blocking the run"
    )
    candidates = [
        Candidate(
            c.provider,
            c.rank,
            c.rank == 1,
            ALL_GATED,
            # The chosen row has to say it is running despite its gate, or a
            # reader sees only the rejection and cannot tell what happened.
            f"{c.reason} — running it anyway rather than blocking the run"
            if c.rank == 1
            else c.reason,
            c.model,
            c.reasoning_effort,
            c.quota_ratio,
            c.success_rate,
            c.calls,
        )
        for c in candidates
    ]
    return Decision(
        fallback.provider, ALL_GATED, reason, candidates,
        fallback.model, fallback.reasoning_effort, complexity,
    )
