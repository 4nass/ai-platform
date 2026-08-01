"""Tests for core.orchestrator.router — which provider serves a role, and why."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.errors import ConfigError
from core.orchestrator import platform_config as pc
from core.orchestrator import router
from core.telemetry import store
from core.telemetry.quota import Budget

KNOWN = {"claude_code", "codex_cli", "anthropic_api", "openai_api"}
THRESHOLDS = router.Thresholds(
    max_quota_ratio=0.85, min_success_rate=0.6, min_samples=5, window_hours=24
)


def _profile(repo_root: Path, body: str, name: str = "test") -> None:
    (repo_root / "config/presets/profiles").mkdir(parents=True, exist_ok=True)
    (repo_root / "config/presets/profiles" / f"{name}.yaml").write_text(body, encoding="utf-8")


def _calls(repo_root: Path, agent: str, provider: str, *, successes: int, failures: int = 0,
           tokens: int = 0, model: str = "", effort: str = "") -> None:
    now = datetime.now(timezone.utc).isoformat()
    with store.connect(repo_root) as con:
        for success in [1] * successes + [0] * failures:
            con.execute(
                "INSERT INTO calls(run_id, agent, provider, model, reasoning_effort, success, input_tokens,"
                " cache_read_tokens, cache_creation_tokens, output_tokens, started_at, duration_ms)"
                " VALUES(1,?,?,?,?,?, ?,0,0,0,?,100)",
                (agent, provider, model, effort, success, tokens, now),
            )


def _route(
    repo_root: Path,
    agent: str = "reviewer",
    complexity: str = router.DEFAULT_COMPLEXITY,
    *,
    quotas: dict[str, Budget] | None = None,
) -> router.Decision:
    """Builds a PlatformConfig in memory rather than writing a fake
    config/platform.yaml + config/presets tree for every case: only the
    profile preset actually needs to exist on disk (eligible_profiles reads a
    named file), quotas/thresholds are just data now."""
    config = pc.PlatformConfig(
        profile="test", quotas=quotas or {}, routing=THRESHOLDS
    )
    return router.route(repo_root, agent, KNOWN, platform_config=config, complexity=complexity)


PREFERENCE = "reviewer:\n  providers: [codex_cli, claude_code]\n"


# --- the declared preference governs ---


def test_takes_the_first_declared_provider_when_nothing_gates_it(tmp_path: Path) -> None:
    _profile(tmp_path, PREFERENCE)

    assert _route(tmp_path).provider == "codex_cli"


def test_cold_start_says_so_rather_than_implying_a_measurement(tmp_path: Path) -> None:
    """Most role/provider pairs sit here for a long time. A reason that reads
    like a metric-driven choice when there is no data would be a lie."""
    _profile(tmp_path, PREFERENCE)

    decision = _route(tmp_path)

    assert decision.rule == router.NO_HISTORY
    assert "no recorded history" in decision.reason


def test_the_reason_carries_the_numbers_it_decided_on(tmp_path: Path) -> None:
    _profile(tmp_path, PREFERENCE)
    _calls(tmp_path, "reviewer", "codex_cli", successes=10)

    decision = _route(tmp_path)

    assert decision.rule == router.PREFERRED
    assert "100% success over 10 calls" in decision.reason


def test_a_bare_provider_key_still_works(tmp_path: Path) -> None:
    """Old configs keep running; the migration to a list is mechanical."""
    _profile(tmp_path, "reviewer:\n  provider: claude_code\n")

    assert _route(tmp_path).provider == "claude_code"


def test_legacy_provider_entry_can_be_augmented_with_profile_fields(tmp_path: Path) -> None:
    _profile(
        tmp_path,
        "reviewer:\n  provider: codex_cli\n  model: gpt-x\n  reasoning_effort: minimal\n",
    )

    decision = _route(tmp_path)

    assert (decision.model, decision.reasoning_effort) == ("gpt-x", "minimal")


def test_profile_exposes_model_effort_and_distinct_candidate_identity(tmp_path: Path) -> None:
    _profile(tmp_path, """reviewer:
  profiles:
    - {provider: codex_cli, model: gpt-fast, reasoning_effort: low}
    - {provider: codex_cli, model: gpt-deep, reasoning_effort: high}
""")

    decision = _route(tmp_path)

    assert (decision.provider, decision.model, decision.reasoning_effort) == (
        "codex_cli", "gpt-fast", "low"
    )
    assert [(c.model, c.reasoning_effort) for c in decision.candidates] == [
        ("gpt-fast", "low"), ("gpt-deep", "high")
    ]
    assert "gpt-fast/low" in decision.reason


def test_failure_gate_is_scoped_to_the_exact_profile(tmp_path: Path) -> None:
    _profile(tmp_path, """reviewer:
  profiles:
    - {provider: codex_cli, model: gpt-fast, reasoning_effort: low}
    - {provider: codex_cli, model: gpt-deep, reasoning_effort: high}
""")
    _calls(tmp_path, "reviewer", "codex_cli", successes=0, failures=6,
           model="gpt-fast", effort="low")
    _calls(tmp_path, "reviewer", "codex_cli", successes=6,
           model="gpt-deep", effort="high")

    decision = _route(tmp_path)

    assert (decision.model, decision.reasoning_effort) == ("gpt-deep", "high")
    assert decision.candidates[0].rule == router.FAILING_ROLE


def test_invalid_codex_effort_is_a_config_error(tmp_path: Path) -> None:
    _profile(tmp_path, "reviewer:\n  profiles: [{provider: codex_cli, reasoning_effort: extreme}]\n")

    with pytest.raises(ConfigError, match="Unsupported codex_cli effort"):
        _route(tmp_path)



def test_canonical_effort_key_is_exposed_as_the_execution_effort(tmp_path: Path) -> None:
    _profile(
        tmp_path,
        "reviewer:\n  profiles: [{provider: claude_code, model: claude-opus-5, effort: ultracode}]\n",
    )

    decision = _route(tmp_path)

    assert (decision.model, decision.reasoning_effort) == ("claude-opus-5", "ultracode")


def test_complexity_override_replaces_the_base_profile_list(tmp_path: Path) -> None:
    _profile(
        tmp_path,
        """reviewer:
  profiles:
    - {provider: codex_cli, model: gpt-5.6-sol, effort: high}
  profiles_by_complexity:
    routine:
      - {provider: codex_cli, model: gpt-5.6-terra, effort: low}
    critical:
      - {provider: claude_code, model: claude-opus-5, effort: ultracode}
""",
    )

    routine = _route(tmp_path, complexity="routine")
    critical = _route(tmp_path, complexity="critical")
    default = _route(tmp_path)

    assert (routine.model, routine.reasoning_effort) == ("gpt-5.6-terra", "low")
    assert (critical.provider, critical.model, critical.reasoning_effort) == (
        "claude_code", "claude-opus-5", "ultracode"
    )
    assert (default.model, default.reasoning_effort) == ("gpt-5.6-sol", "high")
    assert critical.complexity == "critical"


def test_unknown_complexity_is_a_config_error(tmp_path: Path) -> None:
    _profile(tmp_path, PREFERENCE)

    with pytest.raises(ConfigError, match="Unsupported task complexity"):
        _route(tmp_path, complexity="legendary")


def test_unknown_complexity_override_key_is_a_config_error(tmp_path: Path) -> None:
    _profile(
        tmp_path,
        """reviewer:
  profiles: [{provider: codex_cli}]
  profiles_by_complexity:
    extreme: [{provider: claude_code}]
""",
    )

    with pytest.raises(ConfigError, match="unknown complexity profile"):
        _route(tmp_path)



def test_complexity_overrides_must_be_a_mapping_even_when_empty(tmp_path: Path) -> None:
    _profile(
        tmp_path,
        """reviewer:
  profiles: [{provider: codex_cli}]
  profiles_by_complexity: []
""",
    )

    with pytest.raises(ConfigError, match="expected a mapping"):
        _route(tmp_path)

def test_profile_cannot_declare_both_effort_spellings(tmp_path: Path) -> None:
    _profile(
        tmp_path,
        """reviewer:
  profiles:
    - {provider: codex_cli, effort: high, reasoning_effort: high}
""",
    )

    with pytest.raises(ConfigError, match="declares both"):
        _route(tmp_path)


def test_invalid_claude_effort_is_a_config_error(tmp_path: Path) -> None:
    _profile(tmp_path, "reviewer:\n  profiles: [{provider: claude_code, effort: extreme}]\n")

    with pytest.raises(ConfigError, match="Unsupported claude_code effort"):
        _route(tmp_path)


# --- the shipped presets (config/presets/profiles/*.yaml) ---


def test_dogfood_policy_has_claude_and_codex_profiles_for_every_role_and_tier() -> None:
    engine_root = Path(__file__).parents[1]
    roles = {
        "decomposer",
        "architect",
        "backend",
        "frontend",
        "reviewer",
        "security",
        "tests",
        "documentation",
        "corrector",
    }

    for role in roles:
        for complexity in router.COMPLEXITIES:
            profiles = router.eligible_profiles(engine_root, role, complexity, profile="balanced")

            assert {profile.provider for profile in profiles} == {"claude_code", "codex_cli"}
            assert all(profile.model for profile in profiles)
            assert all(profile.reasoning_effort for profile in profiles)


def test_dogfood_policy_is_calibrated_for_pro_without_automatic_ultracode() -> None:
    engine_root = Path(__file__).parents[1]
    expected_first_profiles = {
        ("architect", "routine"): ("codex_cli", "gpt-5.6-terra", "medium"),
        ("architect", "complex"): ("codex_cli", "gpt-5.6-sol", "high"),
        ("architect", "critical"): ("codex_cli", "gpt-5.6-sol", "xhigh"),
        ("security", "routine"): ("codex_cli", "gpt-5.6-sol", "medium"),
        ("security", "complex"): ("codex_cli", "gpt-5.6-sol", "high"),
        ("security", "critical"): ("codex_cli", "gpt-5.6-sol", "xhigh"),
    }

    for (role, complexity), expected in expected_first_profiles.items():
        profile = router.eligible_profiles(engine_root, role, complexity, profile="balanced")[0]

        assert (profile.provider, profile.model, profile.reasoning_effort) == expected

    roles = (
        "decomposer", "architect", "backend", "frontend", "reviewer",
        "security", "tests", "documentation", "corrector",
    )
    all_efforts = {
        profile.reasoning_effort
        for role in roles
        for complexity in router.COMPLEXITIES
        for profile in router.eligible_profiles(engine_root, role, complexity, profile="balanced")
    }
    assert "ultracode" not in all_efforts


def test_max_preset_uses_each_roles_most_capable_profile_unconditionally() -> None:
    """max is a mechanical promotion of balanced's own critical tier, not new
    tuning -- confirm it actually differs from balanced and matches what
    balanced already declared as critical."""
    engine_root = Path(__file__).parents[1]

    balanced_critical = router.eligible_profiles(
        engine_root, "architect", "critical", profile="balanced"
    )
    max_default = router.eligible_profiles(engine_root, "architect", "complex", profile="max")

    assert [(p.provider, p.model, p.reasoning_effort) for p in max_default] == [
        (p.provider, p.model, p.reasoning_effort) for p in balanced_critical
    ]


# --- gate 1: quota pressure ---


def test_a_provider_over_its_quota_ceiling_is_skipped(tmp_path: Path) -> None:
    """The whole point of reasoning in quota rather than dollars."""
    _profile(tmp_path, PREFERENCE)
    _calls(tmp_path, "reviewer", "codex_cli", successes=1, tokens=950)  # 95% of 1000

    decision = _route(tmp_path, quotas={"codex_cli": Budget(window_hours=24, tokens=1000)})

    assert decision.provider == "claude_code"
    assert "over the 85% ceiling" in decision.candidates[0].reason


def test_a_provider_under_its_ceiling_is_kept(tmp_path: Path) -> None:
    _profile(tmp_path, PREFERENCE)
    _calls(tmp_path, "reviewer", "codex_cli", successes=1, tokens=500)  # 50%

    decision = _route(tmp_path, quotas={"codex_cli": Budget(window_hours=24, tokens=1000)})

    assert decision.provider == "codex_cli"


def test_a_provider_with_no_declared_budget_is_never_gated_on_quota(tmp_path: Path) -> None:
    """An undeclared budget is unknown, not zero — it must not read as
    'exhausted' and silently retire a provider."""
    _profile(tmp_path, PREFERENCE)
    _calls(tmp_path, "reviewer", "codex_cli", successes=1, tokens=10_000_000)

    assert _route(tmp_path, quotas={}).provider == "codex_cli"


# --- gate 2: the failure floor, gated on sample size ---


def test_a_provider_failing_this_role_is_skipped(tmp_path: Path) -> None:
    _profile(tmp_path, PREFERENCE)
    _calls(tmp_path, "reviewer", "codex_cli", successes=1, failures=9)

    decision = _route(tmp_path)

    assert decision.provider == "claude_code"
    assert "below the 60% floor" in decision.candidates[0].reason


def test_a_bad_run_on_too_few_calls_does_not_retire_a_provider(tmp_path: Path) -> None:
    """0.5 over 2 calls and 0.5 over 200 are not the same claim. Without the
    sample gate, one bad call out of two would retire a provider from a role."""
    _profile(tmp_path, PREFERENCE)
    _calls(tmp_path, "reviewer", "codex_cli", successes=1, failures=1)

    assert _route(tmp_path).provider == "codex_cli"


def test_failures_on_another_role_do_not_count_against_this_one(tmp_path: Path) -> None:
    """The floor is per role: a provider can be poor at writing code and fine
    at deciding which tasks to run."""
    _profile(tmp_path, PREFERENCE)
    _calls(tmp_path, "backend", "codex_cli", successes=0, failures=10)

    assert _route(tmp_path).provider == "codex_cli"


# --- the never-block guarantee ---


def test_every_candidate_gated_still_yields_a_provider(tmp_path: Path) -> None:
    """A tool driven from a phone must not refuse to work because a config
    threshold was crossed. Degrading loudly beats failing."""
    _profile(tmp_path, PREFERENCE)
    _calls(tmp_path, "reviewer", "codex_cli", successes=1, tokens=5000)
    _calls(tmp_path, "reviewer", "claude_code", successes=1, tokens=5000)

    decision = _route(
        tmp_path,
        quotas={
            "codex_cli": Budget(window_hours=24, tokens=100),
            "claude_code": Budget(window_hours=24, tokens=100),
        },
    )

    assert decision.provider == "codex_cli"  # the declared first choice
    assert decision.rule == router.ALL_GATED
    assert "rather than blocking the run" in decision.reason


def test_the_chosen_row_says_it_ran_despite_its_gate(tmp_path: Path) -> None:
    """Otherwise a reader sees only the rejection and cannot tell what happened."""
    _profile(tmp_path, "reviewer:\n  providers: [codex_cli]\n")
    _calls(tmp_path, "reviewer", "codex_cli", successes=1, tokens=5000)

    (candidate,) = _route(
        tmp_path, quotas={"codex_cli": Budget(window_hours=24, tokens=100)}
    ).candidates

    assert candidate.chosen is True
    assert "running it anyway" in candidate.reason


# --- every candidate is accounted for ---


def test_candidates_not_reached_are_still_reported(tmp_path: Path) -> None:
    """A router that only shows its winner cannot be audited."""
    _profile(tmp_path, PREFERENCE)

    decision = _route(tmp_path)

    assert [c.provider for c in decision.candidates] == ["codex_cli", "claude_code"]
    assert decision.candidates[1].reason.startswith("not reached")


# --- configuration errors ---


def test_an_unknown_role_is_a_config_error(tmp_path: Path) -> None:
    _profile(tmp_path, PREFERENCE)

    with pytest.raises(ConfigError, match="Unknown agent role"):
        _route(tmp_path, "nope")


def test_a_role_declaring_no_providers_is_a_config_error(tmp_path: Path) -> None:
    _profile(tmp_path, "reviewer: {}\n")

    with pytest.raises(ConfigError, match="declares no providers"):
        _route(tmp_path)


def test_an_unknown_provider_name_is_a_config_error(tmp_path: Path) -> None:
    _profile(tmp_path, "reviewer:\n  providers: [not_a_provider]\n")

    with pytest.raises(ConfigError, match="Unknown provider"):
        _route(tmp_path)


def test_an_unknown_profile_preset_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Unknown profile preset 'nonexistent'"):
        router.eligible_profiles(tmp_path, "reviewer", profile="nonexistent")


# --- route() self-loads a PlatformConfig when none is given ---


def test_route_self_loads_platform_config_when_none_is_given(tmp_path: Path) -> None:
    """Standalone callers (the CLI commands, most other tests) should not
    have to construct a PlatformConfig by hand for the common case."""
    (tmp_path / "config/presets/profiles").mkdir(parents=True)
    (tmp_path / "config/presets/profiles/balanced.yaml").write_text(PREFERENCE, encoding="utf-8")
    (tmp_path / "config/presets/workflow").mkdir(parents=True)
    (tmp_path / "config/presets/workflow/standard.yaml").write_text("tasks: []\n", encoding="utf-8")
    (tmp_path / "config/presets/context").mkdir(parents=True)
    (tmp_path / "config/presets/context/smart.yaml").write_text("use_git_diff: true\n", encoding="utf-8")

    decision = router.route(tmp_path, "reviewer", KNOWN)

    assert decision.provider == "codex_cli"
