"""Tests for core.telemetry.quota — declared budgets vs recorded consumption."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from core.telemetry import store
from core.telemetry.quota import Budget, pressure

BUDGETS = {
    "claude_code": Budget(window_hours=5.0, tokens=1000),
    "codex_cli": Budget(window_hours=5.0, tokens=2000),
}


def _record(repo_root: Path, provider: str, tokens: int) -> None:
    with store.connect(repo_root) as con:
        con.execute(
            "INSERT INTO calls(run_id, agent, provider, success, input_tokens, cache_read_tokens,"
            " cache_creation_tokens, output_tokens, started_at, duration_ms)"
            " VALUES(1,'a',?,1,?,0,0,0,?,100)",
            (provider, tokens, datetime.now(timezone.utc).isoformat()),
        )


def test_pressure_reports_consumption_against_the_declared_budget(tmp_path: Path) -> None:
    _record(tmp_path, "codex_cli", 500)

    rows = {r["provider"]: r for r in pressure(tmp_path, BUDGETS)}

    assert rows["codex_cli"]["total_tokens"] == 500
    assert rows["codex_cli"]["used_ratio"] == 0.25  # 500 / 2000


def test_a_provider_with_a_budget_and_no_traffic_still_appears(tmp_path: Path) -> None:
    """Idle is a routing signal; its absence reads as missing data."""
    _record(tmp_path, "codex_cli", 500)

    rows = {r["provider"]: r for r in pressure(tmp_path, BUDGETS)}

    assert rows["claude_code"]["calls"] == 0
    assert rows["claude_code"]["used_ratio"] == 0.0


def test_usage_without_a_declared_budget_reports_no_ratio(tmp_path: Path) -> None:
    """None is 'not declared', distinct from 0.0 which would claim idleness."""
    _record(tmp_path, "codex_cli", 500)

    (row,) = pressure(tmp_path, {})

    assert row["total_tokens"] == 500
    assert row["used_ratio"] is None


def test_no_declared_budgets_is_not_an_error(tmp_path: Path) -> None:
    """Consumption is still worth reporting; no run should be blocked because
    the subscriber hasn't declared their plan limits yet."""
    _record(tmp_path, "codex_cli", 500)

    (row,) = pressure(tmp_path, {})

    assert row["total_tokens"] == 500
    assert row["budget_tokens"] is None
