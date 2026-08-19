from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import security
from core.jobs import store
from core.telemetry import store as telemetry
from providers.base import ProviderResult


def test_builtin_canaries_are_deterministically_redacted() -> None:
    canary = "sk-proj-12345678901234567890 Bearer abcdefghijklmnop password=super-secret-value"
    redactor = security.Redactor()
    first = redactor.text(canary)
    assert first == redactor.text(canary)
    assert "12345678901234567890" not in first
    assert "abcdefghijklmnop" not in first
    assert "super-secret-value" not in first
    assert "[REDACTED]" in first


def test_project_patterns_are_applied_without_logging_the_match(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "platform.yaml").write_text(
        "security:\n  redaction_patterns: ['CANARY-[A-Z0-9]+']\n  retention:\n    runs_days: 3\n",
        encoding="utf-8",
    )
    redactor = security.redactor(tmp_path)
    assert redactor.text("value CANARY-ABC123") == "value [REDACTED:CUSTOM]"
    assert security.load_policy(tmp_path).retention.runs_days == 3


def test_telemetry_never_persists_canary_secret(tmp_path: Path) -> None:
    secret = "ghp_123456789012345678901234"
    recorder = telemetry.RunRecorder(
        tmp_path, f"request with {secret}", metadata={"note": secret}
    )
    recorder.record_call(
        agent="backend",
        provider="fake",
        result=ProviderResult(
            success=False, summary=f"provider failed: {secret}", raw={"error": secret}
        ),
        metadata={"diagnostic": secret},
    )
    with sqlite3.connect(tmp_path / "telemetry.sqlite") as con:
        values = [str(value) for row in con.execute("SELECT * FROM runs") for value in row]
        values += [str(value) for row in con.execute("SELECT * FROM calls") for value in row]
    assert all(secret not in value for value in values)


def test_job_queue_preserves_the_executable_request_but_telemetry_redacts_it(
    tmp_path: Path,
) -> None:
    secret = "sk-test-12345678901234567890"
    request = f"fix {secret}"
    submission = store.submit(
        tmp_path,
        project=str(tmp_path),
        request=request,
        envelope={"request": secret},
    )
    assert submission.created
    assert store.get(tmp_path, submission.id).request == request

    recorder = telemetry.RunRecorder(tmp_path, request, metadata={"request": secret})
    recorder.finish(summary=f"completed {secret}")
    with telemetry.connect(tmp_path) as con:
        values = [str(value) for row in con.execute("SELECT * FROM runs") for value in row]
    assert all(secret not in value for value in values)


def test_delete_run_leaves_auditable_tombstone(tmp_path: Path) -> None:
    recorder = telemetry.RunRecorder(tmp_path, "safe request", session_id="session-1")
    recorder.finish(summary="done")
    assert telemetry.delete_run(tmp_path, recorder.run_id, actor="owner") == 1
    assert telemetry.recent_runs(tmp_path) == []
    with telemetry.connect(tmp_path) as con:
        row = con.execute("SELECT scope, selector, actor FROM tombstones").fetchone()
    assert tuple(row) == ("run", str(recorder.run_id), "owner")


def test_sqlite_files_are_owner_only(tmp_path: Path) -> None:
    with telemetry.connect(tmp_path):
        pass
    with store.connect(tmp_path):
        pass
    assert (tmp_path / "telemetry.sqlite").stat().st_mode & 0o077 == 0
    assert (tmp_path / "jobs.sqlite").stat().st_mode & 0o077 == 0


def test_retention_purges_old_rows_and_reports_unimplemented_artifacts(tmp_path: Path) -> None:
    recorder = telemetry.RunRecorder(tmp_path, "old request")
    recorder.finish(summary="done")
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    with telemetry.connect(tmp_path) as con:
        con.execute("UPDATE runs SET finished_at = ? WHERE id = ?", (old, recorder.run_id))
    counts = telemetry.purge_expired(
        tmp_path, security.RetentionPolicy(runs_days=1, calls_days=1, events_days=1)
    )
    assert counts["runs"] == 1
    assert counts["diffs"] == counts["attachments"] == 0


def test_artifact_directory_helper_is_owner_only(tmp_path: Path) -> None:
    path = security.secure_directory(tmp_path / "artifacts")
    assert path.is_dir()
    assert path.stat().st_mode & 0o077 == 0

def test_retention_purges_completed_jobs_and_settled_reservations(tmp_path: Path) -> None:
    from core.jobs import budget

    submission = store.submit(tmp_path, project=str(tmp_path), request="safe request")
    assert store.cancel(tmp_path, submission.id)
    reservation = budget.reserve(tmp_path, run_key="completed", estimated=10)
    budget.settle(tmp_path, reservation, actual=5)
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    with store.connect(tmp_path) as con:
        con.execute(
            "UPDATE jobs SET finished_at = ? WHERE id = ?", (old, submission.id)
        )
        con.execute(
            "UPDATE reservations SET settled_at = ? WHERE id = ?", (old, reservation)
        )

    counts = telemetry.purge_expired(
        tmp_path, security.RetentionPolicy(runs_days=1, calls_days=1, events_days=1)
    )

    assert counts["jobs"] == 1
    assert counts["reservations"] == 1
    assert store.recent(tmp_path) == []


def test_zero_retention_keeps_completed_queue_records(tmp_path: Path) -> None:
    from core.jobs import budget

    submission = store.submit(tmp_path, project=str(tmp_path), request="safe request")
    assert store.cancel(tmp_path, submission.id)
    reservation = budget.reserve(tmp_path, run_key="keep", estimated=10)
    budget.settle(tmp_path, reservation, actual=5)

    counts = telemetry.purge_expired(
        tmp_path, security.RetentionPolicy(runs_days=0, calls_days=0, events_days=0)
    )

    assert counts["jobs"] == counts["reservations"] == 0
    assert len(store.recent(tmp_path)) == 1

def test_delete_tombstones_use_the_engine_redaction_policy(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "platform.yaml").write_text(
        "security:\n  redaction_patterns: ['CANARY-[A-Z0-9]+']\n",
        encoding="utf-8",
    )

    telemetry.delete_session(tmp_path, "CANARY-SESSION", actor="CANARY-ACTOR")

    with telemetry.connect(tmp_path) as con:
        row = con.execute("SELECT selector, actor FROM tombstones").fetchone()
    assert tuple(row) == ("[REDACTED:CUSTOM]", "[REDACTED:CUSTOM]")


def test_retention_removes_stale_incomplete_telemetry_runs(tmp_path: Path) -> None:
    recorder = telemetry.RunRecorder(tmp_path, "incomplete")
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    with telemetry.connect(tmp_path) as con:
        con.execute("UPDATE runs SET started_at = ? WHERE id = ?", (old, recorder.run_id))

    counts = telemetry.purge_expired(
        tmp_path, security.RetentionPolicy(runs_days=1, calls_days=1, events_days=1)
    )

    assert counts["runs"] == 1
    assert telemetry.recent_runs(tmp_path) == []
