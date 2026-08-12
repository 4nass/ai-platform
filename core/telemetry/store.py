"""The engine's analytical memory: what every run and every provider call cost.

This is deliberately not a log. The schema exists to answer questions about
the engine's own behavior — which provider is cheapest for a kind of task,
which model succeeds most often, what the graph actually saves, why a model
was chosen, and which change to the engine improved things. Columns that only
later steps will populate (`routing_reason`, `context_reason`) are here from
the start because a decision can be recorded as it happens or not at all;
it can never be reconstructed afterwards.

SQLite via the stdlib — no server, no new dependency. Writes open a
short-lived connection each time rather than sharing one: the DAG stages run
in worker threads (see core.orchestrator.supervisor), and a sqlite3
connection is not safe to share across them. WAL plus a busy timeout makes
concurrent writers a non-issue at this volume.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from providers.base import ProviderResult
from core import security

DB_PATH = Path("telemetry.sqlite")
BUSY_TIMEOUT_SECONDS = 10.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  session_id    TEXT,
  target_repo   TEXT,
  request       TEXT,
  branch        TEXT,
  summary       TEXT,
  engine_commit TEXT,
  started_at    TEXT,
  finished_at   TEXT,
  duration_ms   INTEGER,
  metadata      TEXT
);

CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY,
  run_id   INTEGER REFERENCES runs(id),
  stage_id TEXT,
  agent    TEXT,
  provider TEXT,
  model    TEXT,
  reasoning_effort TEXT,
  success  INTEGER,
  input_tokens          INTEGER,
  output_tokens         INTEGER,
  cache_read_tokens     INTEGER,
  cache_creation_tokens INTEGER,
  cost_usd  REAL,
  started_at TEXT,
  finished_at TEXT,
  duration_ms INTEGER,
  provider_duration_ms INTEGER,
  context_files INTEGER,
  context_chars INTEGER,
  routing_reason TEXT,
  context_reason TEXT,
  metadata TEXT
);

CREATE INDEX IF NOT EXISTS idx_calls_run    ON calls(run_id);
CREATE INDEX IF NOT EXISTS idx_calls_agent  ON calls(agent, provider);
CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id);

CREATE TABLE IF NOT EXISTS tombstones (
  id INTEGER PRIMARY KEY,
  scope TEXT NOT NULL,
  selector TEXT NOT NULL,
  deleted_at TEXT NOT NULL,
  actor TEXT NOT NULL DEFAULT '',
  rows_deleted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tombstones_scope ON tombstones(scope);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _migrate(con: sqlite3.Connection, engine_root: Path) -> None:
    """Adds columns to pre-existing tables that `CREATE TABLE IF NOT
    EXISTS` can't retrofit. Guarded so it's a no-op on a fresh database (the
    schema above already declares the column) and idempotent on repeated
    calls.

    Every row that predates `--repo` was, by construction, a self-targeting
    run — `target_root` and `engine_root` were always the same directory
    before this column existed. Backfilling those rows with `engine_root`
    (rather than leaving them NULL) means `recent_runs(..., target_repo=...)`
    can filter safely without silently hiding a repo's own pre-migration
    history the first time it's queried.
    """
    columns = {row["name"] for row in con.execute("PRAGMA table_info(runs)")}
    if "target_repo" not in columns:
        con.execute("ALTER TABLE runs ADD COLUMN target_repo TEXT")
        con.execute(
            "UPDATE runs SET target_repo = ? WHERE target_repo IS NULL OR target_repo = ''",
            (str(engine_root),),
        )

    call_columns = {row["name"] for row in con.execute("PRAGMA table_info(calls)")}
    if "reasoning_effort" not in call_columns:
        con.execute("ALTER TABLE calls ADD COLUMN reasoning_effort TEXT")


@contextmanager
def connect(engine_root: Path):
    """Opens the telemetry DB, creating it and its schema on first use.

    Bound to `engine_root`, never to a target repo's worktree: this is the
    engine's own shared analytical memory (which provider is cheapest, which
    model succeeds most often), invariant across every project the engine is
    pointed at via `--repo`. Which project a given run touched is recorded
    per-row (`runs.target_repo`), not by moving the database.
    """
    path = engine_root / DB_PATH
    con = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS)
    try:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)
        _migrate(con, engine_root)
        yield con
        con.commit()
    finally:
        con.close()


class RunRecorder:
    """Records one run and every provider call it makes.

    Always bind this to `engine_root`, never to a task worktree or to
    `target_root`: DAG stages run against a throwaway worktree that gets
    deleted when the stage finishes, and the telemetry has to outlive it —
    and it's the engine's own shared memory, not a per-project log (see
    `connect`). `target_repo` records which project this particular run was
    against, so `recent_runs` can still be scoped per project even though the
    database is shared.
    """

    def __init__(
        self,
        engine_root: Path,
        request: str,
        *,
        target_repo: str = "",
        session_id: str | None = None,
        engine_commit: str = "",
        metadata: dict | None = None,
    ) -> None:
        self.engine_root = engine_root
        self._redactor = security.redactor(
            engine_root, Path(target_repo) if target_repo else None
        )
        request = self._redactor.text(request)
        metadata = self._redactor.value(metadata or {})
        self._started_at = _now()
        with connect(engine_root) as con:
            cursor = con.execute(
                "INSERT INTO runs(session_id, target_repo, request, engine_commit, started_at, metadata) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (session_id, target_repo, request, engine_commit, self._started_at, json.dumps(metadata)),
            )
            self.run_id = cursor.lastrowid

    def record_call(
        self,
        *,
        agent: str,
        provider: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
        result: ProviderResult,
        stage_id: str | None = None,
        context_files: int = 0,
        context_chars: int = 0,
        duration_ms: int | None = None,
        started_at: str | None = None,
        routing_reason: str = "",
        context_reason: str = "",
        metadata: dict | None = None,
    ) -> None:
        usage = result.usage
        result = self._redactor.result(result)
        routing_reason = self._redactor.text(routing_reason)
        context_reason = self._redactor.text(context_reason)
        metadata = self._redactor.value(metadata or {})
        with connect(self.engine_root) as con:
            con.execute(
                "INSERT INTO calls("
                " run_id, stage_id, agent, provider, model, reasoning_effort, success,"
                " input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,"
                " cost_usd, started_at, finished_at, duration_ms, provider_duration_ms,"
                " context_files, context_chars, routing_reason, context_reason, metadata"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.run_id,
                    stage_id,
                    agent,
                    provider,
                    model or (usage.model if usage else ""),
                    reasoning_effort or "",
                    1 if result.success else 0,
                    usage.input_tokens if usage else 0,
                    usage.output_tokens if usage else 0,
                    usage.cache_read_tokens if usage else 0,
                    usage.cache_creation_tokens if usage else 0,
                    usage.cost_usd if usage else None,
                    started_at,
                    _now(),
                    duration_ms,
                    usage.provider_duration_ms if usage else None,
                    context_files,
                    context_chars,
                    routing_reason,
                    context_reason,
                    json.dumps(metadata),
                ),
            )

    def finish(self, *, branch: str = "", summary: str = "") -> None:
        finished_at = _now()
        started = datetime.fromisoformat(self._started_at)
        duration_ms = int((datetime.fromisoformat(finished_at) - started).total_seconds() * 1000)
        with connect(self.engine_root) as con:
            con.execute(
                "UPDATE runs SET branch = ?, summary = ?, finished_at = ?, duration_ms = ? WHERE id = ?",
                (self._redactor.text(branch), self._redactor.text(summary), finished_at, duration_ms, self.run_id),
            )


def run_totals(engine_root: Path, run_id: int) -> dict:
    """Aggregate cost/tokens for one run — what the CLI prints at the end.

    `priced_calls` is reported alongside `calls` on purpose: a provider that
    reports no cost (anthropic_api) must not make a run look cheaper than it
    was, so the caller can tell "$0.42 across 8 calls" from "$0.42 across the
    3 of 8 calls that reported a price".
    """
    with connect(engine_root) as con:
        row = con.execute(
            "SELECT COUNT(*) AS calls, COUNT(cost_usd) AS priced_calls,"
            " COALESCE(SUM(cost_usd), 0) AS cost_usd,"
            " COALESCE(SUM(input_tokens), 0) AS input_tokens,"
            " COALESCE(SUM(output_tokens), 0) AS output_tokens,"
            " COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,"
            " COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens"
            " FROM calls WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return dict(row)


def provider_pressure(engine_root: Path, *, window_hours: float, provider: str | None = None) -> list[dict]:
    """Per-provider consumption over a rolling window — the routing signal.

    Both providers are flat-rate subscriptions, so a per-call price measures
    nothing the subscriber can act on; what binds is quota. Neither CLI
    reports how much allowance is left (codex emits only thread/turn/item
    events; claude reports a price but no balance), so pressure is derived
    from what was actually recorded here.

    Deliberately reports `calls` alongside every average: "0.9 success over 2
    calls" and "0.9 over 200" are not the same claim, and a router that can't
    tell them apart will chase noise.
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    query = (
        "SELECT provider,"
        " COUNT(*) AS calls,"
        " COALESCE(SUM(input_tokens + cache_read_tokens + cache_creation_tokens), 0) AS input_tokens,"
        " COALESCE(SUM(output_tokens), 0) AS output_tokens,"
        " COALESCE(SUM(input_tokens + cache_read_tokens + cache_creation_tokens + output_tokens), 0)"
        "   AS total_tokens,"
        " ROUND(AVG(success), 3) AS success_rate,"
        " CAST(AVG(duration_ms) AS INTEGER) AS avg_duration_ms"
        " FROM calls WHERE started_at >= ?"
    )
    params: list[object] = [since]
    if provider is not None:
        query += " AND provider = ?"
        params.append(provider)
    query += " GROUP BY provider ORDER BY total_tokens DESC"

    with connect(engine_root) as con:
        return [dict(row) for row in con.execute(query, params)]


def role_performance(
    engine_root: Path, agent: str, *, window_hours: float, provider: str | None = None,
    model: str | None = None, reasoning_effort: str | None = None,
) -> dict[str, dict]:
    """How each provider has actually done on one role, keyed by provider.

    The router's second gate reads this. `calls` is returned alongside every
    average for the same reason as in provider_pressure: a success rate with
    no sample size behind it invites a policy built on two data points. When
    a provider is supplied, model and effort scope the query to that exact
    execution profile; omitted values match legacy/provider-neutral rows.
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    rows = {}
    with connect(engine_root) as con:
        query = (
            "SELECT provider,"
            " COUNT(*) AS calls,"
            " ROUND(AVG(success), 3) AS success_rate,"
            " CAST(AVG(duration_ms) AS INTEGER) AS avg_duration_ms,"
            " CAST(AVG(input_tokens + cache_read_tokens + cache_creation_tokens + output_tokens)"
            "   AS INTEGER) AS avg_tokens"
            " FROM calls WHERE agent = ? AND started_at >= ?"
        )
        params: list[object] = [agent, since]
        if provider is not None:
            query += " AND provider = ?"
            params.append(provider)
        if model is not None:
            query += " AND model = ?"
            params.append(model)
        elif provider is not None:
            query += " AND COALESCE(model, '') = ''"
        if reasoning_effort is not None:
            query += " AND reasoning_effort = ?"
            params.append(reasoning_effort)
        elif provider is not None:
            query += " AND COALESCE(reasoning_effort, '') = ''"
        query += " GROUP BY provider"
        for row in con.execute(query, params):
            rows[row["provider"]] = dict(row)
    return rows


def recent_runs(
    engine_root: Path, *, limit: int = 20, session_id: str | None = None, target_repo: str | None = None
) -> list[dict]:
    """Recent runs with their rolled-up call totals, newest first.

    Filtered to `target_repo` by callers that resolved one via `--repo` — the
    database is shared across every project (see `connect`), so without this
    a run's history would show every other project's runs mixed in.
    """
    # input_tokens is only the uncached remainder — the real prompt size is
    # that plus the cache reads and writes (see format_totals).
    query = (
        "SELECT r.id, r.session_id, r.target_repo, r.request, r.branch, r.summary, r.started_at,"
        " r.duration_ms, r.engine_commit,"
        " COUNT(c.id) AS calls, COUNT(c.cost_usd) AS priced_calls,"
        " COALESCE(SUM(c.cost_usd), 0) AS cost_usd,"
        " COALESCE(SUM(c.input_tokens + c.cache_read_tokens + c.cache_creation_tokens), 0) AS input_tokens,"
        " COALESCE(SUM(c.cache_read_tokens), 0) AS cache_read_tokens,"
        " COALESCE(SUM(c.output_tokens), 0) AS output_tokens"
        " FROM runs r LEFT JOIN calls c ON c.run_id = r.id"
    )
    clauses: list[str] = []
    params: list[object] = []
    if session_id is not None:
        clauses.append("r.session_id = ?")
        params.append(session_id)
    if target_repo is not None:
        clauses.append("r.target_repo = ?")
        params.append(target_repo)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " GROUP BY r.id ORDER BY r.id DESC LIMIT ?"
    params.append(limit)

    with connect(engine_root) as con:
        return [dict(row) for row in con.execute(query, params)]


def _delete_runs(con: sqlite3.Connection, *, scope: str, selector: str, actor: str = "") -> int:
    """Delete telemetry rows while leaving a non-sensitive audit tombstone."""
    if scope == "run":
        rows = con.execute("SELECT id FROM runs WHERE id = ?", (int(selector),)).fetchall()
    elif scope == "session":
        rows = con.execute("SELECT id FROM runs WHERE session_id = ?", (selector,)).fetchall()
    elif scope == "project":
        rows = con.execute("SELECT id FROM runs WHERE target_repo = ?", (selector,)).fetchall()
    else:
        raise ValueError(f"Unknown deletion scope {scope!r}")
    run_ids = [int(row["id"]) for row in rows]
    if run_ids:
        marks = ",".join("?" for _ in run_ids)
        con.execute(f"DELETE FROM calls WHERE run_id IN ({marks})", run_ids)
        con.execute(f"DELETE FROM runs WHERE id IN ({marks})", run_ids)
    safe_selector = security.redactor(Path(".")).text(selector)
    con.execute(
        "INSERT INTO tombstones(scope, selector, deleted_at, actor, rows_deleted) VALUES(?,?,?,?,?)",
        (scope, safe_selector, _now(), security.redactor(Path(".")).text(actor), len(run_ids)),
    )
    return len(run_ids)


def delete_run(engine_root: Path, run_id: int, *, actor: str = "") -> int:
    with connect(engine_root) as con:
        return _delete_runs(con, scope="run", selector=str(run_id), actor=actor)


def delete_session(engine_root: Path, session_id: str, *, actor: str = "") -> int:
    with connect(engine_root) as con:
        return _delete_runs(con, scope="session", selector=session_id, actor=actor)


def delete_project(engine_root: Path, target_repo: str, *, actor: str = "") -> int:
    with connect(engine_root) as con:
        return _delete_runs(con, scope="project", selector=target_repo, actor=actor)


def purge_expired(engine_root: Path, policy: security.RetentionPolicy | None = None) -> dict[str, int]:
    """Apply retention to the stores currently implemented by the engine.

    Diffs and attachments are returned as zero until those artifact stores
    exist; callers can still expose one stable retention report today.
    """
    policy = policy or security.load_policy(engine_root).retention
    now = datetime.now(timezone.utc)
    counts = {"runs": 0, "calls": 0, "events": 0, "diffs": 0, "attachments": 0}
    with connect(engine_root) as con:
        call_cutoff = (now - timedelta(days=policy.calls_days)).isoformat()
        run_cutoff = (now - timedelta(days=policy.runs_days)).isoformat()
        counts["calls"] = con.execute(
            "DELETE FROM calls WHERE finished_at IS NOT NULL AND finished_at < ?",
            (call_cutoff,),
        ).rowcount
        old_runs = con.execute(
            "SELECT id FROM runs WHERE finished_at IS NOT NULL AND finished_at < ?",
            (run_cutoff,),
        ).fetchall()
        if old_runs:
            ids = [int(row["id"]) for row in old_runs]
            marks = ",".join("?" for _ in ids)
            counts["calls"] += con.execute(
                f"DELETE FROM calls WHERE run_id IN ({marks})", ids
            ).rowcount
            counts["runs"] = con.execute(
                f"DELETE FROM runs WHERE id IN ({marks})", ids
            ).rowcount
    from core.jobs import store as job_store
    with job_store.connect(engine_root) as con:
        event_cutoff = (now - timedelta(days=policy.events_days)).isoformat()
        counts["events"] = con.execute("DELETE FROM job_events WHERE at < ?", (event_cutoff,)).rowcount
        approval_table = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'approval_events'"
        ).fetchone()
        if approval_table:
            counts["events"] += con.execute(
                "DELETE FROM approval_events WHERE at < ?", (event_cutoff,)
            ).rowcount
    return counts
