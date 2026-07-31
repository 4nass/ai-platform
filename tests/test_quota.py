"""Tests for core.telemetry.quota — declared budgets vs recorded consumption."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from core.telemetry import quota, store

QUOTA_YAML = """providers:
  claude_code:
    window_hours: 5
    tokens: 1000
  codex_cli:
    window_hours: 5
    tokens: 2000
"""


def _write_quota(repo_root: Path, body: str = QUOTA_YAML) -> None:
    (repo_root / "config").mkdir(exist_ok=True)
    (repo_root / "config" / "quota.yaml").write_text(body, encoding="utf-8")


def _record(repo_root: Path, provider: str, tokens: int) -> None:
    with store.connect(repo_root) as con:
        con.execute(
            "INSERT INTO calls(run_id, agent, provider, success, input_tokens, cache_read_tokens,"
            " cache_creation_tokens, output_tokens, started_at, duration_ms)"
            " VALUES(1,'a',?,1,?,0,0,0,?,100)",
            (provider, tokens, datetime.now(timezone.utc).isoformat()),
        )


def test_pressure_reports_consumption_against_the_declared_budget(tmp_path: Path) -> None:
    _write_quota(tmp_path)
    _record(tmp_path, "codex_cli", 500)

    rows = {r["provider"]: r for r in quota.pressure(tmp_path)}

    assert rows["codex_cli"]["total_tokens"] == 500
    assert rows["codex_cli"]["used_ratio"] == 0.25  # 500 / 2000


def test_a_provider_with_a_budget_and_no_traffic_still_appears(tmp_path: Path) -> None:
    """Idle is a routing signal; its absence reads as missing data."""
    _write_quota(tmp_path)
    _record(tmp_path, "codex_cli", 500)

    rows = {r["provider"]: r for r in quota.pressure(tmp_path)}

    assert rows["claude_code"]["calls"] == 0
    assert rows["claude_code"]["used_ratio"] == 0.0


def test_usage_without_a_declared_budget_reports_no_ratio(tmp_path: Path) -> None:
    """None is 'not declared', distinct from 0.0 which would claim idleness."""
    _write_quota(tmp_path, "providers: {}\n")
    _record(tmp_path, "codex_cli", 500)

    (row,) = quota.pressure(tmp_path)

    assert row["total_tokens"] == 500
    assert row["used_ratio"] is None


def test_a_missing_quota_file_is_not_an_error(tmp_path: Path) -> None:
    """Consumption is still worth reporting; no run should be blocked because
    the subscriber hasn't written their plan limits down yet."""
    _record(tmp_path, "codex_cli", 500)

    (row,) = quota.pressure(tmp_path)

    assert row["total_tokens"] == 500
    assert row["budget_tokens"] is None


def test_malformed_budget_entries_are_skipped_not_fatal(tmp_path: Path) -> None:
    _write_quota(tmp_path, "providers:\n  codex_cli:\n    tokens: not-a-number\n")

    assert quota.load_budgets(tmp_path) == {}
