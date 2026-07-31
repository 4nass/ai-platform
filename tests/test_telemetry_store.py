"""Tests for core.telemetry.store."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.telemetry import store as telemetry
from providers.base import ProviderResult, TokenUsage


def _result(*, success=True, cost=0.05, model="claude-sonnet-5", tokens=100) -> ProviderResult:
    return ProviderResult(
        success=success,
        summary="done",
        usage=TokenUsage(
            model=model,
            input_tokens=tokens,
            output_tokens=tokens // 10,
            cache_read_tokens=5,
            cache_creation_tokens=3,
            cost_usd=cost,
            provider_duration_ms=1200,
        ),
    )


def test_recorder_creates_run_and_calls(tmp_path: Path) -> None:
    recorder = telemetry.RunRecorder(tmp_path, "add oauth2", session_id="sig-1", engine_commit="abc123")
    recorder.record_call(agent="backend", provider="claude_code", result=_result(), stage_id="backend")
    recorder.finish(branch="engine/x", summary="done")

    with telemetry.connect(tmp_path) as con:
        run = con.execute("SELECT * FROM runs").fetchone()
        call = con.execute("SELECT * FROM calls").fetchone()

    assert run["request"] == "add oauth2"
    assert run["session_id"] == "sig-1"
    assert run["engine_commit"] == "abc123"
    assert run["branch"] == "engine/x"
    assert run["summary"] == "done"
    assert run["duration_ms"] is not None

    assert call["run_id"] == run["id"]
    assert call["stage_id"] == "backend"
    assert call["model"] == "claude-sonnet-5"
    assert call["success"] == 1
    assert call["cost_usd"] == 0.05
    assert call["provider_duration_ms"] == 1200


def test_run_metadata_is_queryable_json(tmp_path: Path) -> None:
    telemetry.RunRecorder(tmp_path, "x", metadata={"use_graph": True, "max_files": 20})

    with telemetry.connect(tmp_path) as con:
        row = con.execute(
            "SELECT json_extract(metadata, '$.use_graph') AS g,"
            " json_extract(metadata, '$.max_files') AS m FROM runs"
        ).fetchone()

    assert row["g"] == 1  # SQLite renders JSON true as 1
    assert row["m"] == 20


def test_call_without_usage_records_zeros_not_null(tmp_path: Path) -> None:
    """A provider that reports nothing must still produce a usable row —
    the call happened, and its absence from the totals would be a lie."""
    recorder = telemetry.RunRecorder(tmp_path, "x")
    recorder.record_call(agent="backend", provider="codex_cli", result=ProviderResult(success=False, summary="boom"))

    with telemetry.connect(tmp_path) as con:
        call = con.execute("SELECT * FROM calls").fetchone()

    assert call["input_tokens"] == 0
    assert call["cost_usd"] is None  # distinct from 0.0: unknown, not free
    assert call["success"] == 0


def test_concurrent_writes_from_threads_all_land(tmp_path: Path) -> None:
    """DAG stages record from ThreadPoolExecutor workers — a shared sqlite3
    connection would raise or drop rows here."""
    recorder = telemetry.RunRecorder(tmp_path, "parallel")
    errors: list[str] = []

    def worker(name: str) -> None:
        try:
            for _ in range(10):
                recorder.record_call(agent=name, provider="claude_code", result=_result(), stage_id=name)
        except Exception as exc:  # noqa: BLE001 - the point is to surface any failure
            errors.append(f"{name}: {type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(f"agent{i}",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    with telemetry.connect(tmp_path) as con:
        count = con.execute("SELECT COUNT(*) FROM calls").fetchone()[0]

    assert errors == []
    assert count == 40


def test_run_totals_aggregates_and_flags_unpriced_calls(tmp_path: Path) -> None:
    recorder = telemetry.RunRecorder(tmp_path, "x")
    recorder.record_call(agent="backend", provider="claude_code", result=_result(cost=0.10, tokens=100))
    recorder.record_call(agent="tests", provider="claude_code", result=_result(cost=0.05, tokens=200))
    recorder.record_call(agent="docs", provider="anthropic_api", result=_result(cost=None, tokens=50))

    totals = telemetry.run_totals(tmp_path, recorder.run_id)

    assert totals["calls"] == 3
    assert totals["priced_calls"] == 2  # the unpriced one must not read as free
    assert totals["cost_usd"] == pytest.approx(0.15)
    assert totals["input_tokens"] == 350


def test_recent_runs_newest_first_and_filterable_by_session(tmp_path: Path) -> None:
    for i in range(3):
        rec = telemetry.RunRecorder(tmp_path, f"req {i}", session_id="whatsapp-42" if i else "other")
        rec.record_call(agent="backend", provider="claude_code", result=_result())
        rec.finish(branch=f"b{i}", summary="done")

    all_runs = telemetry.recent_runs(tmp_path)
    scoped = telemetry.recent_runs(tmp_path, session_id="whatsapp-42")

    assert [r["request"] for r in all_runs] == ["req 2", "req 1", "req 0"]
    assert {r["session_id"] for r in scoped} == {"whatsapp-42"}
    assert len(scoped) == 2


# --- the five questions the schema exists to answer -------------------------


def _seed_analytics(tmp_path: Path) -> None:
    older = telemetry.RunRecorder(
        tmp_path, "no graph", engine_commit="commit-old", metadata={"use_graph": False}
    )
    older.record_call(
        agent="backend", provider="claude_code", result=_result(cost=0.20, tokens=900), context_files=35
    )
    older.finish(summary="done")

    newer = telemetry.RunRecorder(
        tmp_path, "with graph", engine_commit="commit-new", metadata={"use_graph": True}
    )
    newer.record_call(
        agent="backend", provider="claude_code", result=_result(cost=0.08, tokens=300), context_files=7
    )
    newer.record_call(
        agent="backend",
        provider="anthropic_api",
        result=_result(cost=None, model="claude-opus-5", success=False, tokens=100),
        context_files=7,
    )
    newer.finish(summary="done")


def test_q1_cheapest_provider_per_task_type(tmp_path: Path) -> None:
    _seed_analytics(tmp_path)
    with telemetry.connect(tmp_path) as con:
        rows = [
            dict(r)
            for r in con.execute(
                "SELECT agent, provider, ROUND(AVG(cost_usd),4) avg_cost,"
                " COUNT(cost_usd) priced, COUNT(*) total"
                " FROM calls GROUP BY agent, provider ORDER BY agent, avg_cost"
            )
        ]

    by_provider = {r["provider"]: r for r in rows}
    assert by_provider["claude_code"]["avg_cost"] == 0.14
    # the unpriced provider is visible as unpriced, not as free
    assert by_provider["anthropic_api"]["avg_cost"] is None
    assert by_provider["anthropic_api"]["priced"] == 0
    assert by_provider["anthropic_api"]["total"] == 1


def test_q2_success_rate_per_model(tmp_path: Path) -> None:
    _seed_analytics(tmp_path)
    with telemetry.connect(tmp_path) as con:
        rows = {
            r["model"]: r["ok_rate"]
            for r in con.execute(
                "SELECT model, ROUND(AVG(success),3) ok_rate FROM calls GROUP BY model"
            )
        }

    assert rows["claude-sonnet-5"] == 1.0
    assert rows["claude-opus-5"] == 0.0


def test_q3_what_the_graph_actually_saves(tmp_path: Path) -> None:
    _seed_analytics(tmp_path)
    with telemetry.connect(tmp_path) as con:
        rows = {
            r["graph"]: dict(r)
            for r in con.execute(
                "SELECT json_extract(r.metadata,'$.use_graph') AS graph,"
                " ROUND(AVG(c.context_files),1) files, ROUND(AVG(c.input_tokens),0) in_tok"
                " FROM calls c JOIN runs r ON r.id = c.run_id GROUP BY graph"
            )
        }

    assert rows[0]["files"] == 35 and rows[0]["in_tok"] == 900
    assert rows[1]["files"] == 7 and rows[1]["in_tok"] == 200


def test_q4_why_this_model_was_chosen(tmp_path: Path) -> None:
    recorder = telemetry.RunRecorder(tmp_path, "x")
    recorder.record_call(
        agent="backend",
        provider="claude_code",
        result=_result(),
        stage_id="backend",
        routing_reason="diff <50 lines, no architecture change",
    )

    with telemetry.connect(tmp_path) as con:
        row = con.execute(
            "SELECT stage_id, agent, model, routing_reason FROM calls WHERE run_id = ?",
            (recorder.run_id,),
        ).fetchone()

    assert row["routing_reason"] == "diff <50 lines, no architecture change"


def test_q5_which_engine_change_improved_things(tmp_path: Path) -> None:
    _seed_analytics(tmp_path)
    with telemetry.connect(tmp_path) as con:
        rows = {
            r["engine_commit"]: dict(r)
            for r in con.execute(
                "SELECT r.engine_commit, COUNT(DISTINCT r.id) runs,"
                " ROUND(AVG(c.cost_usd),4) cost, ROUND(AVG(c.success),3) ok_rate"
                " FROM runs r JOIN calls c ON c.run_id = r.id"
                " GROUP BY r.engine_commit ORDER BY MIN(r.started_at)"
            )
        }

    assert rows["commit-old"]["cost"] == 0.20
    assert rows["commit-new"]["cost"] == 0.08  # only the priced call counts
    assert rows["commit-new"]["ok_rate"] == 0.5


def test_metadata_defaults_to_empty_json_not_null(tmp_path: Path) -> None:
    """json_extract on NULL yields NULL, which would silently drop rows from
    every analytical GROUP BY — default to '{}' so absent metadata still
    groups."""
    telemetry.RunRecorder(tmp_path, "x")
    with telemetry.connect(tmp_path) as con:
        assert json.loads(con.execute("SELECT metadata FROM runs").fetchone()[0]) == {}


# --- provider pressure: the quota substrate that replaces dollar reasoning ---


def _call(con, provider: str, *, input_tokens: int = 0, cache_read: int = 0, output: int = 0,
          success: int = 1, duration_ms: int = 1000, hours_ago: float = 0.0) -> None:
    started = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    con.execute(
        "INSERT INTO calls(run_id, agent, provider, success, input_tokens, cache_read_tokens,"
        " cache_creation_tokens, output_tokens, started_at, duration_ms)"
        " VALUES(1,'a',?,?,?,?,0,?,?,?)",
        (provider, success, input_tokens, cache_read, output, started, duration_ms),
    )


def test_provider_pressure_totals_the_true_prompt_size_per_provider(tmp_path: Path) -> None:
    """Sums all three input fields, matching the TokenUsage convention every
    adapter normalizes into — otherwise providers aren't comparable."""
    with telemetry.connect(tmp_path) as con:
        _call(con, "claude_code", input_tokens=100, cache_read=900, output=50)
        _call(con, "codex_cli", input_tokens=200, cache_read=0, output=10)

    rows = {r["provider"]: r for r in telemetry.provider_pressure(tmp_path, window_hours=5)}

    assert rows["claude_code"]["input_tokens"] == 1000
    assert rows["claude_code"]["total_tokens"] == 1050
    assert rows["codex_cli"]["total_tokens"] == 210


def test_provider_pressure_ignores_calls_outside_the_window(tmp_path: Path) -> None:
    """Quota is a rolling allowance — consumption from last week does not
    press on this window."""
    with telemetry.connect(tmp_path) as con:
        _call(con, "codex_cli", input_tokens=100, hours_ago=0.5)
        _call(con, "codex_cli", input_tokens=9999, hours_ago=48)

    (row,) = telemetry.provider_pressure(tmp_path, window_hours=5)

    assert row["total_tokens"] == 100
    assert row["calls"] == 1


def test_provider_pressure_reports_success_rate_and_sample_size(tmp_path: Path) -> None:
    """The count travels with the rate: 0.5 over 2 calls and 0.5 over 200 are
    not the same claim, and a router that can't tell them apart chases noise."""
    with telemetry.connect(tmp_path) as con:
        _call(con, "codex_cli", success=1, duration_ms=1000)
        _call(con, "codex_cli", success=0, duration_ms=3000)

    (row,) = telemetry.provider_pressure(tmp_path, window_hours=5)

    assert row["success_rate"] == 0.5
    assert row["calls"] == 2
    assert row["avg_duration_ms"] == 2000


def test_provider_pressure_can_filter_to_one_provider(tmp_path: Path) -> None:
    with telemetry.connect(tmp_path) as con:
        _call(con, "claude_code", input_tokens=100)
        _call(con, "codex_cli", input_tokens=200)

    rows = telemetry.provider_pressure(tmp_path, window_hours=5, provider="codex_cli")

    assert [r["provider"] for r in rows] == ["codex_cli"]
