"""Which provider serves a role, and why.

Retrieval proposes, an arbiter decides, every decision leaves a reason — the
same shape as core.context.selection, applied to providers instead of files.

The authority is deliberately limited. The preference order in
config/agents.yaml governs; this module overrides it only on two things it can
measure — quota pressure and repeated failure on a large enough sample. It does
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

import yaml

from core.errors import ConfigError
from core.telemetry import quota as quota_store
from core.telemetry import store as telemetry

AGENTS_CONFIG_PATH = Path("config/agents.yaml")
ROUTING_CONFIG_PATH = Path("config/routing.yaml")

PREFERRED = "preferred"
NO_HISTORY = "no_history"
OVER_QUOTA = "over_quota"
FAILING_ROLE = "failing_role"
ALL_GATED = "all_gated"
CODEX_REASONING_EFFORTS = {"minimal", "low", "medium", "high", "xhigh"}


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


def load_thresholds(engine_root: Path) -> Thresholds:
    path = engine_root / ROUTING_CONFIG_PATH
    if not path.is_file():
        return Thresholds()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Thresholds(
        **{k: v for k, v in data.items() if k in Thresholds.__dataclass_fields__}
    )


@dataclass(frozen=True)
class ExecutionProfile:
    provider: str
    model: str | None = None
    reasoning_effort: str | None = None

    @property
    def label(self) -> str:
        details = "/".join(v for v in (self.model, self.reasoning_effort) if v)
        return f"{self.provider} ({details})" if details else self.provider


def eligible_profiles(engine_root: Path, agent: str) -> list[ExecutionProfile]:
    """The declared preference order for a role.

    Accepts either `providers: [a, b]` or the older `provider: a`, which reads
    as a one-element list — the migration is mechanical and old configs keep
    working.
    """
    config = yaml.safe_load((engine_root / AGENTS_CONFIG_PATH).read_text(encoding="utf-8")) or {}

    if agent not in config:
        known = ", ".join(sorted(config)) or "(none configured)"
        raise ConfigError(f"Unknown agent role '{agent}'. Configured roles: {known}")

    entry = config[agent] or {}
    declared = entry.get("profiles") or entry.get("providers")
    if not declared and entry.get("provider"):
        declared = [{
            key: entry[key]
            for key in ("provider", "model", "reasoning_effort")
            if key in entry
        }]
    if not declared:
        raise ConfigError(
            f"Agent role '{agent}' declares no providers in {AGENTS_CONFIG_PATH}. "
            "Set `profiles: [{provider: name, ...}]` or `providers: [name, ...]`."
        )
    profiles = []
    for item in declared:
        if isinstance(item, str):
            profiles.append(ExecutionProfile(item))
        elif isinstance(item, dict) and item.get("provider"):
            profile = ExecutionProfile(
                str(item["provider"]), item.get("model"), item.get("reasoning_effort")
            )
            if (
                profile.provider == "codex_cli"
                and profile.reasoning_effort is not None
                and profile.reasoning_effort not in CODEX_REASONING_EFFORTS
            ):
                raise ConfigError(
                    f"Unsupported Codex reasoning_effort {profile.reasoning_effort!r} "
                    f"for agent '{agent}'"
                )
            profiles.append(profile)
        else:
            raise ConfigError(f"Invalid execution profile for agent '{agent}': {item!r}")
    return profiles


def eligible_providers(repo_root: Path, agent: str) -> list[str]:
    """Compatibility view of :func:`eligible_profiles`."""
    return [profile.provider for profile in eligible_profiles(repo_root, agent)]


def route(
    engine_root: Path,
    agent: str,
    known_providers: set[str],
    *,
    thresholds: Thresholds | None = None,
) -> Decision:
    """Picks a provider for this role and explains the choice.

    `engine_root` is the ai-platform install, never a target repo's worktree:
    provider eligibility, thresholds and the recorded history this reads are
    all engine-scoped, not per-project, so they stay fixed no matter which
    repo a task is currently operating on.

    Never returns without a provider. If every candidate is gated, the declared
    first choice runs anyway and the reason says so: a tool driven from a phone
    must not refuse to work because a config threshold was crossed. Degrading
    loudly beats failing.
    """
    thresholds = thresholds or load_thresholds(engine_root)
    declared = eligible_profiles(engine_root, agent)

    unknown = [p.provider for p in declared if p.provider not in known_providers]
    if unknown:
        known = ", ".join(sorted(known_providers))
        raise ConfigError(
            f"Unknown provider(s) {', '.join(unknown)} for agent '{agent}'. Available: {known}"
        )

    pressure = {
        row["provider"]: row
        for row in quota_store.pressure(engine_root, window_hours=thresholds.window_hours)
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
            chosen.model, chosen.reasoning_effort,
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
        fallback.model, fallback.reasoning_effort,
    )
