"""Tests for core.orchestrator.router — which provider serves a role, and why."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.errors import ConfigError
from core.orchestrator import router
from core.telemetry import store

KNOWN = {"claude_code", "codex_cli", "anthropic_api", "openai_api"}
THRESHOLDS = router.Thresholds(
    max_quota_ratio=0.85, min_success_rate=0.6, min_samples=5, window_hours=24
)


def _config(repo_root: Path, agents: str, quota: str = "", routing: str = "") -> None:
    (repo_root / "config").mkdir(exist_ok=True)
    (repo_root / "config" / "agents.yaml").write_text(agents, encoding="utf-8")
    if quota:
        (repo_root / "config" / "quota.yaml").write_text(quota, encoding="utf-8")
    if routing:
        (repo_root / "config" / "routing.yaml").write_text(routing, encoding="utf-8")


def _calls(repo_root: Path, agent: str, provider: str, *, successes: int, failures: int = 0,
           tokens: int = 0) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with store.connect(repo_root) as con:
        for success in [1] * successes + [0] * failures:
            con.execute(
                "INSERT INTO calls(run_id, agent, provider, success, input_tokens,"
                " cache_read_tokens, cache_creation_tokens, output_tokens, started_at, duration_ms)"
                " VALUES(1,?,?,?,?,0,0,0,?,100)",
                (agent, provider, success, tokens, now),
            )


def _route(repo_root: Path, agent: str = "reviewer") -> router.Decision:
    return router.route(repo_root, agent, KNOWN, thresholds=THRESHOLDS)


PREFERENCE = "reviewer:\n  providers: [codex_cli, claude_code]\n"


# --- the declared preference governs ---


def test_takes_the_first_declared_provider_when_nothing_gates_it(tmp_path: Path) -> None:
    _config(tmp_path, PREFERENCE)

    assert _route(tmp_path).provider == "codex_cli"


def test_cold_start_says_so_rather_than_implying_a_measurement(tmp_path: Path) -> None:
    """Most role/provider pairs sit here for a long time. A reason that reads
    like a metric-driven choice when there is no data would be a lie."""
    _config(tmp_path, PREFERENCE)

    decision = _route(tmp_path)

    assert decision.rule == router.NO_HISTORY
    assert "no recorded history" in decision.reason


def test_the_reason_carries_the_numbers_it_decided_on(tmp_path: Path) -> None:
    _config(tmp_path, PREFERENCE)
    _calls(tmp_path, "reviewer", "codex_cli", successes=10)

    decision = _route(tmp_path)

    assert decision.rule == router.PREFERRED
    assert "100% success over 10 calls" in decision.reason


def test_a_bare_provider_key_still_works(tmp_path: Path) -> None:
    """Old configs keep running; the migration to a list is mechanical."""
    _config(tmp_path, "reviewer:\n  provider: claude_code\n")

    assert _route(tmp_path).provider == "claude_code"


# --- gate 1: quota pressure ---


QUOTA = "providers:\n  codex_cli:\n    window_hours: 24\n    tokens: 1000\n"


def test_a_provider_over_its_quota_ceiling_is_skipped(tmp_path: Path) -> None:
    """The whole point of reasoning in quota rather than dollars."""
    _config(tmp_path, PREFERENCE, quota=QUOTA)
    _calls(tmp_path, "reviewer", "codex_cli", successes=1, tokens=950)  # 95% of 1000

    decision = _route(tmp_path)

    assert decision.provider == "claude_code"
    assert "over the 85% ceiling" in decision.candidates[0].reason


def test_a_provider_under_its_ceiling_is_kept(tmp_path: Path) -> None:
    _config(tmp_path, PREFERENCE, quota=QUOTA)
    _calls(tmp_path, "reviewer", "codex_cli", successes=1, tokens=500)  # 50%

    assert _route(tmp_path).provider == "codex_cli"


def test_a_provider_with_no_declared_budget_is_never_gated_on_quota(tmp_path: Path) -> None:
    """An undeclared budget is unknown, not zero — it must not read as
    'exhausted' and silently retire a provider."""
    _config(tmp_path, PREFERENCE, quota="providers: {}\n")
    _calls(tmp_path, "reviewer", "codex_cli", successes=1, tokens=10_000_000)

    assert _route(tmp_path).provider == "codex_cli"


# --- gate 2: the failure floor, gated on sample size ---


def test_a_provider_failing_this_role_is_skipped(tmp_path: Path) -> None:
    _config(tmp_path, PREFERENCE)
    _calls(tmp_path, "reviewer", "codex_cli", successes=1, failures=9)

    decision = _route(tmp_path)

    assert decision.provider == "claude_code"
    assert "below the 60% floor" in decision.candidates[0].reason


def test_a_bad_run_on_too_few_calls_does_not_retire_a_provider(tmp_path: Path) -> None:
    """0.5 over 2 calls and 0.5 over 200 are not the same claim. Without the
    sample gate, one bad call out of two would retire a provider from a role."""
    _config(tmp_path, PREFERENCE)
    _calls(tmp_path, "reviewer", "codex_cli", successes=1, failures=1)

    assert _route(tmp_path).provider == "codex_cli"


def test_failures_on_another_role_do_not_count_against_this_one(tmp_path: Path) -> None:
    """The floor is per role: a provider can be poor at writing code and fine
    at deciding which tasks to run."""
    _config(tmp_path, PREFERENCE)
    _calls(tmp_path, "backend", "codex_cli", successes=0, failures=10)

    assert _route(tmp_path).provider == "codex_cli"


# --- the never-block guarantee ---


def test_every_candidate_gated_still_yields_a_provider(tmp_path: Path) -> None:
    """A tool driven from a phone must not refuse to work because a config
    threshold was crossed. Degrading loudly beats failing."""
    _config(
        tmp_path,
        PREFERENCE,
        quota=(
            "providers:\n  codex_cli:\n    window_hours: 24\n    tokens: 100\n"
            "  claude_code:\n    window_hours: 24\n    tokens: 100\n"
        ),
    )
    _calls(tmp_path, "reviewer", "codex_cli", successes=1, tokens=5000)
    _calls(tmp_path, "reviewer", "claude_code", successes=1, tokens=5000)

    decision = _route(tmp_path)

    assert decision.provider == "codex_cli"  # the declared first choice
    assert decision.rule == router.ALL_GATED
    assert "rather than blocking the run" in decision.reason


def test_the_chosen_row_says_it_ran_despite_its_gate(tmp_path: Path) -> None:
    """Otherwise a reader sees only the rejection and cannot tell what happened."""
    _config(
        tmp_path,
        "reviewer:\n  providers: [codex_cli]\n",
        quota="providers:\n  codex_cli:\n    window_hours: 24\n    tokens: 100\n",
    )
    _calls(tmp_path, "reviewer", "codex_cli", successes=1, tokens=5000)

    (candidate,) = _route(tmp_path).candidates

    assert candidate.chosen is True
    assert "running it anyway" in candidate.reason


# --- every candidate is accounted for ---


def test_candidates_not_reached_are_still_reported(tmp_path: Path) -> None:
    """A router that only shows its winner cannot be audited."""
    _config(tmp_path, PREFERENCE)

    decision = _route(tmp_path)

    assert [c.provider for c in decision.candidates] == ["codex_cli", "claude_code"]
    assert decision.candidates[1].reason.startswith("not reached")


# --- configuration errors ---


def test_an_unknown_role_is_a_config_error(tmp_path: Path) -> None:
    _config(tmp_path, PREFERENCE)

    with pytest.raises(ConfigError, match="Unknown agent role"):
        _route(tmp_path, "nope")


def test_a_role_declaring_no_providers_is_a_config_error(tmp_path: Path) -> None:
    _config(tmp_path, "reviewer: {}\n")

    with pytest.raises(ConfigError, match="declares no providers"):
        _route(tmp_path)


def test_an_unknown_provider_name_is_a_config_error(tmp_path: Path) -> None:
    _config(tmp_path, "reviewer:\n  providers: [not_a_provider]\n")

    with pytest.raises(ConfigError, match="Unknown provider"):
        _route(tmp_path)


# --- thresholds ---


def test_thresholds_come_from_config(tmp_path: Path) -> None:
    _config(tmp_path, PREFERENCE, routing="max_quota_ratio: 0.5\nmin_samples: 2\n")

    thresholds = router.load_thresholds(tmp_path)

    assert (thresholds.max_quota_ratio, thresholds.min_samples) == (0.5, 2)


def test_a_missing_routing_config_falls_back_to_defaults(tmp_path: Path) -> None:
    assert router.load_thresholds(tmp_path) == router.Thresholds()
