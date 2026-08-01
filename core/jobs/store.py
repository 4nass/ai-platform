"""The engine's job queue: what it has been asked to do, and where each ask got to.

`ai-platform run` is synchronous — it holds a terminal for the length of a
run and its state dies with the process. That is fine for a CLI in front of
someone and useless for a caller that submits work, disconnects, and comes
back later on a different device (issue #24). This module is the durable half:
a submission is persisted *before* it is acknowledged, execution happens
somewhere else, and the outcome outlives both.

**Why a separate database from `telemetry.sqlite`.** They answer different
questions and have opposite write patterns. Telemetry is append-only
analytical memory — "what did the engine do and what did it cost" — and every
query over it (`recent_runs`, `run_totals`, `role_performance`,
`provider_pressure`) assumes a row means *a run happened*. A job that is
queued, cancelled before it started, or interrupted is not a run; putting it
in `runs` would mean adding a state filter to every one of those queries and
getting it wrong somewhere. Jobs are also mutable and hot: a heartbeat rewrites
the row every few seconds for the whole life of a run. `jobs.run_id` points at
the telemetry row once execution actually starts, which is the only link the
two need.

**Idempotency.** Every transition is a no-op when the job is already in the
target state: a worker that crashes after writing `succeeded` but before
acknowledging can safely say it again. Transitions that aren't in `TRANSITIONS`
raise rather than silently corrupting the lifecycle — a job going from
`succeeded` back to `running` is a bug in the caller, not a state to record.
"""

from __future__ import annotations

import json
import socket
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path("jobs.sqlite")
BUSY_TIMEOUT_SECONDS = 10.0

QUEUED = "queued"
RUNNING = "running"
WAITING_APPROVAL = "waiting_approval"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
INTERRUPTED = "interrupted"

TERMINAL_STATES = frozenset({SUCCEEDED, FAILED, CANCELLED, INTERRUPTED})
ACTIVE_STATES = frozenset({QUEUED, RUNNING, WAITING_APPROVAL})

TRANSITIONS: dict[str, frozenset[str]] = {
    QUEUED: frozenset({RUNNING, CANCELLED}),
    # back to `queued` on purpose: a target repo already being mutated by
    # another run is a scheduling conflict, not a failure of this job (see
    # git_ops.exclusive_run_lock). Returning it to the queue is the difference
    # between a queue and a fire-once trigger.
    RUNNING: frozenset({WAITING_APPROVAL, SUCCEEDED, FAILED, CANCELLED, INTERRUPTED, QUEUED}),
    WAITING_APPROVAL: frozenset({RUNNING, CANCELLED, FAILED, INTERRUPTED}),
    SUCCEEDED: frozenset(),
    FAILED: frozenset(),
    CANCELLED: frozenset(),
    # The one terminal state that can be left, and only deliberately (see
    # `resume`). An interrupted job is finished in the sense that nothing is
    # working on it, but unlike the other three its work is still on a branch
    # and still completable, so refusing to reopen it would mean throwing away
    # every stage it merged. Nothing reopens it on its own: reconciliation only
    # ever moves jobs *into* this state.
    INTERRUPTED: frozenset({QUEUED}),
}

STALE_AFTER_SECONDS = 180.0
"""How long a `running` job may go without a heartbeat before reconciliation
calls it interrupted. Generous relative to `HEARTBEAT_SECONDS` in
core.jobs.worker: a machine under load must not have a healthy run declared
dead, and the cost of noticing a real crash slightly late is nil — nothing
else is waiting on that answer."""

# Columns a worker may update as a run progresses. Whitelisted rather than
# interpolated freely: `record_progress` takes its keys from the supervisor,
# and a supervisor field that happens to collide with a column name should
# fail loudly here instead of writing somewhere unintended.
PROGRESS_FIELDS = frozenset(
    {"run_id", "base_ref", "base_sha", "branch", "integration_root", "stage", "attempt"}
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  state TEXT NOT NULL,

  -- immutable request envelope: what was asked, by whom, through what.
  -- Never rewritten after submission -- a run that changed its own request
  -- or its own target would make every later audit meaningless.
  project          TEXT NOT NULL,
  request          TEXT NOT NULL,
  channel          TEXT NOT NULL DEFAULT 'cli',
  submitted_by     TEXT NOT NULL DEFAULT '',
  envelope         TEXT NOT NULL DEFAULT '{}',

  -- execution state, filled in as the run progresses
  run_id           INTEGER,
  base_ref         TEXT NOT NULL DEFAULT '',
  base_sha         TEXT NOT NULL DEFAULT '',
  branch           TEXT NOT NULL DEFAULT '',
  integration_root TEXT NOT NULL DEFAULT '',
  stage            TEXT NOT NULL DEFAULT '',
  attempt          INTEGER NOT NULL DEFAULT 0,
  worker_pid       INTEGER,
  worker_host      TEXT NOT NULL DEFAULT '',

  submitted_at     TEXT NOT NULL,
  started_at       TEXT,
  heartbeat_at     TEXT,
  finished_at      TEXT,

  summary          TEXT NOT NULL DEFAULT '',
  detail           TEXT NOT NULL DEFAULT ''
);

-- Append-only. The `jobs` row says where a job is now; this says how it got
-- there, which is the only thing that can answer "why is this interrupted"
-- after the process that interrupted it is gone.
CREATE TABLE IF NOT EXISTS job_events (
  id INTEGER PRIMARY KEY,
  job_id     INTEGER NOT NULL REFERENCES jobs(id),
  from_state TEXT,
  to_state   TEXT NOT NULL,
  at         TEXT NOT NULL,
  note       TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_jobs_state    ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_jobs_project  ON jobs(project);
CREATE INDEX IF NOT EXISTS idx_events_job    ON job_events(job_id);
"""


class JobError(Exception):
    """An illegal lifecycle operation — an unknown job, or a transition the
    state machine doesn't allow."""


@dataclass(frozen=True)
class Job:
    """One submission, as it currently stands. Frozen: this is a snapshot read
    out of the database, not a handle — a worker that mutated it would be
    editing a copy while another process edited the row."""

    id: int
    state: str
    project: str
    request: str
    channel: str
    submitted_by: str
    envelope: dict
    run_id: int | None
    base_ref: str
    base_sha: str
    branch: str
    integration_root: str
    stage: str
    attempt: int
    worker_pid: int | None
    worker_host: str
    submitted_at: str
    started_at: str | None
    heartbeat_at: str | None
    finished_at: str | None
    summary: str
    detail: str

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def is_stale(self, *, stale_after_seconds: float = STALE_AFTER_SECONDS) -> bool:
        """Whether a `running` job has stopped reporting for itself.

        Only meaningful for `running`: a queued job has no worker to hear from,
        and a terminal one has nothing left to say.
        """
        if self.state != RUNNING or not self.heartbeat_at:
            return False
        age = datetime.now(timezone.utc) - datetime.fromisoformat(self.heartbeat_at)
        return age.total_seconds() > stale_after_seconds


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job(row: sqlite3.Row) -> Job:
    data = dict(row)
    data["envelope"] = json.loads(data.get("envelope") or "{}")
    return Job(**data)


@contextmanager
def connect(engine_root: Path):
    """Opens the job database, creating it and its schema on first use.

    Bound to `engine_root`, like the telemetry store and for the same reason:
    the queue is the engine's, not any one project's. Which project a job
    targets is a column.
    """
    con = sqlite3.connect(engine_root / DB_PATH, timeout=BUSY_TIMEOUT_SECONDS)
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        con.executescript(SCHEMA)
        yield con
        con.commit()
    finally:
        con.close()


def submit(
    engine_root: Path,
    *,
    project: str,
    request: str,
    channel: str = "cli",
    submitted_by: str = "",
    envelope: dict | None = None,
) -> int:
    """Persists a submission and returns its id. Contacts no provider.

    This is the whole point of the durable lifecycle: the caller gets an
    identifier it can come back to, before anything expensive, revocable or
    failable has been attempted.
    """
    with connect(engine_root) as con:
        cursor = con.execute(
            "INSERT INTO jobs(state, project, request, channel, submitted_by, envelope, submitted_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (
                QUEUED,
                project,
                request,
                channel,
                submitted_by,
                json.dumps(envelope or {}),
                _now(),
            ),
        )
        job_id = cursor.lastrowid
        con.execute(
            "INSERT INTO job_events(job_id, from_state, to_state, at, note) VALUES(?,?,?,?,?)",
            (job_id, None, QUEUED, _now(), f"submitted via {channel}"),
        )
        return job_id


def get(engine_root: Path, job_id: int) -> Job:
    with connect(engine_root) as con:
        row = con.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise JobError(f"No job {job_id}")
    return _job(row)


def recent(
    engine_root: Path,
    *,
    limit: int = 20,
    state: str | None = None,
    project: str | None = None,
) -> list[Job]:
    query = "SELECT * FROM jobs"
    where, params = [], []
    if state:
        where.append("state = ?")
        params.append(state)
    if project:
        where.append("project = ?")
        params.append(project)
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with connect(engine_root) as con:
        return [_job(row) for row in con.execute(query, params)]


def events(engine_root: Path, job_id: int) -> list[dict]:
    with connect(engine_root) as con:
        return [
            dict(row)
            for row in con.execute(
                "SELECT from_state, to_state, at, note FROM job_events WHERE job_id = ? ORDER BY id",
                (job_id,),
            )
        ]


def transition(
    engine_root: Path, job_id: int, to_state: str, *, note: str = "", **fields
) -> bool:
    """Moves a job to `to_state`, or confirms it is already there.

    Returns True if the state changed, False if the job was already in
    `to_state` (idempotent — no duplicate event, no rewritten timestamps).
    Raises `JobError` for a transition the state machine doesn't allow, which
    is always a caller bug: silently accepting `succeeded -> running` would
    make the audit trail lie about what happened.

    `fields` are progress columns applied in the same transaction, so a worker
    can never record "running" without also recording which run it is running.
    """
    if to_state not in TRANSITIONS:
        raise JobError(f"Unknown job state {to_state!r}")

    with connect(engine_root) as con:
        row = con.execute("SELECT state FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise JobError(f"No job {job_id}")
        current = row["state"]
        if current == to_state:
            return False
        if to_state not in TRANSITIONS[current]:
            allowed = ", ".join(sorted(TRANSITIONS[current])) or "nothing (terminal state)"
            raise JobError(
                f"Job {job_id} cannot go from {current} to {to_state}; allowed: {allowed}"
            )

        now = _now()
        assignments, params = ["state = ?"], [to_state]
        if to_state == RUNNING:
            # Re-set on every entry to `running`, not only the first: a requeued
            # job that starts again is a new attempt at execution, and a
            # heartbeat left over from the previous worker would make it look
            # stale the moment it started.
            assignments += ["started_at = COALESCE(started_at, ?)", "heartbeat_at = ?"]
            params += [now, now]
        if to_state == QUEUED:
            # A job back in the queue has not finished and has no worker.
            # Leaving either behind would show a resumable job as one that
            # already ended, owned by a pid that is gone.
            assignments += ["finished_at = NULL", "worker_pid = NULL"]
        if to_state in TERMINAL_STATES:
            assignments += ["finished_at = ?", "heartbeat_at = NULL"]
            params.append(now)
        for key, value in _checked(fields).items():
            assignments.append(f"{key} = ?")
            params.append(value)

        params.append(job_id)
        con.execute(f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ?", params)
        con.execute(
            "INSERT INTO job_events(job_id, from_state, to_state, at, note) VALUES(?,?,?,?,?)",
            (job_id, current, to_state, now, note),
        )
    return True


def _checked(fields: dict) -> dict:
    unknown = set(fields) - PROGRESS_FIELDS
    if unknown:
        raise JobError(f"Not job progress fields: {', '.join(sorted(unknown))}")
    return fields


def claim(engine_root: Path, job_id: int, *, worker_pid: int) -> Job | None:
    """Takes ownership of a queued job, or returns None if someone else did.

    The guard is in the UPDATE (`WHERE state = 'queued'`) rather than in a
    read-then-write, so two workers racing for the same job resolve in SQLite:
    exactly one sees `rowcount == 1`. Checking first and writing after would
    let both pass the check.
    """
    now = _now()
    with connect(engine_root) as con:
        cursor = con.execute(
            "UPDATE jobs SET state = ?, started_at = COALESCE(started_at, ?), heartbeat_at = ?,"
            " worker_pid = ?, worker_host = ? WHERE id = ? AND state = ?",
            (RUNNING, now, now, worker_pid, socket.gethostname(), job_id, QUEUED),
        )
        if cursor.rowcount != 1:
            return None
        con.execute(
            "INSERT INTO job_events(job_id, from_state, to_state, at, note) VALUES(?,?,?,?,?)",
            (job_id, QUEUED, RUNNING, now, f"claimed by pid {worker_pid}"),
        )
        return _job(con.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())


def claim_next(engine_root: Path, *, worker_pid: int, project: str | None = None) -> Job | None:
    """Claims the oldest queued job, skipping any another worker wins first."""
    while True:
        query = "SELECT id FROM jobs WHERE state = ?"
        params: list = [QUEUED]
        if project:
            query += " AND project = ?"
            params.append(project)
        query += " ORDER BY id LIMIT 1"
        with connect(engine_root) as con:
            row = con.execute(query, params).fetchone()
        if row is None:
            return None
        job = claim(engine_root, row["id"], worker_pid=worker_pid)
        if job is not None:
            return job
        # lost the race for that one; the next iteration picks up whatever is
        # still queued, and terminates when nothing is


def heartbeat(engine_root: Path, job_id: int) -> None:
    """Says the worker is still alive. Scoped to `running` so a heartbeat that
    arrives after the run finished can't resurrect a terminal job's liveness."""
    with connect(engine_root) as con:
        con.execute(
            "UPDATE jobs SET heartbeat_at = ? WHERE id = ? AND state = ?",
            (_now(), job_id, RUNNING),
        )


def record_progress(engine_root: Path, job_id: int, **fields) -> None:
    """Persists where a run has got to, without changing its state.

    This is what makes an interrupted job worth anything: the branch, the
    integration worktree and the stage it died in are on disk and recoverable,
    but only if something wrote down where they are before the process died.
    """
    fields = _checked(fields)
    if not fields:
        return
    assignments = ", ".join(f"{key} = ?" for key in fields)
    with connect(engine_root) as con:
        con.execute(
            f"UPDATE jobs SET {assignments} WHERE id = ?", [*fields.values(), job_id]
        )


def reconcile(engine_root: Path, *, stale_after_seconds: float = STALE_AFTER_SECONDS) -> list[Job]:
    """Marks abandoned jobs interrupted. Returns the ones it changed.

    A WSL restart, a killed terminal or a hard crash leaves rows saying
    `running` forever, and a queue whose "running" doesn't mean running is
    worse than no queue. The signal is heartbeat age rather than a liveness
    check on `worker_pid`: pids are only meaningful on the host that issued
    them and get reused, whereas a stale heartbeat means the same thing
    everywhere.

    Interrupted, not failed: the run's branch and integration worktree still
    exist, and its merged stages are still on that branch, so its work is
    recoverable rather than lost. Marking is still all this does — picking the
    run back up is `resume`, which is deliberate and never automatic. A worker
    that re-queued crashed jobs by itself would retry, in a loop, exactly the
    runs most likely to crash the next worker too.
    """
    changed: list[Job] = []
    for job in recent(engine_root, limit=1000, state=RUNNING):
        if not job.is_stale(stale_after_seconds=stale_after_seconds):
            continue
        transition(
            engine_root,
            job.id,
            INTERRUPTED,
            note=f"no heartbeat since {job.heartbeat_at} — worker presumed gone",
        )
        changed.append(get(engine_root, job.id))
    return changed


def resume(engine_root: Path, job_id: int) -> bool:
    """Puts an interrupted job back in the queue, keeping its identity.

    The same job rather than a new one: its request, its target, its branch and
    the integration worktree holding its merged stages are all still the ones
    being worked on, and a fresh submission would abandon every part of that
    while claiming to be a retry. `ai-platform status` then shows one job whose
    history reads interrupted -> queued -> running, which is what actually
    happened.

    Only `interrupted`. A `failed` job is one the engine ran to completion and
    judged; re-queueing it would re-run a workflow that already reached a
    verdict. What it needs is a new request describing the fix, not another
    pass at the same one.
    """
    job = get(engine_root, job_id)
    if job.state != INTERRUPTED:
        raise JobError(
            f"Job {job_id} is {job.state}, not {INTERRUPTED} — only a job whose worker "
            "died mid-run has work left to pick up. Submit a new request instead."
        )
    return transition(
        engine_root, job_id, QUEUED, note=f"resumed from {job.branch or 'no branch'}"
    )


def cancel(engine_root: Path, job_id: int) -> bool:
    """Cancels a job that hasn't started executing.

    Deliberately refuses a `running` job rather than pretending: nothing can
    currently interrupt a run mid-DAG, and marking the row `cancelled` while
    provider calls kept spending quota would be a lie the queue tells about
    itself. Cooperative cancellation of an in-flight run is issue #29.
    """
    job = get(engine_root, job_id)
    if job.is_terminal:
        return False
    if job.state == RUNNING:
        raise JobError(
            f"Job {job_id} is already running and cannot be stopped mid-run yet "
            "(cooperative cancellation is issue #29). Let it finish, or kill its worker "
            f"(pid {job.worker_pid}) and let reconciliation mark it interrupted."
        )
    return transition(engine_root, job_id, CANCELLED, note="cancelled before execution")


def purge_older_than(engine_root: Path, *, days: float) -> int:
    """Drops terminal jobs past their useful life. Active jobs are never
    touched, whatever their age — an old `running` row is reconciliation's
    problem, not something to delete out from under a live worker."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with connect(engine_root) as con:
        ids = [
            row["id"]
            for row in con.execute(
                f"SELECT id FROM jobs WHERE state IN ({','.join('?' * len(TERMINAL_STATES))})"
                " AND finished_at < ?",
                (*sorted(TERMINAL_STATES), cutoff),
            )
        ]
        for job_id in ids:
            con.execute("DELETE FROM job_events WHERE job_id = ?", (job_id,))
            con.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    return len(ids)
