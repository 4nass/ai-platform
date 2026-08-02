"""Tests for core.jobs.store — the durable run lifecycle (issue #24).

The properties that matter here are the ones a crash exercises: a submission
is on disk before anyone is told it exists, a transition means the same thing
whether it is applied once or twice, two workers cannot both own one job, and
a machine that dies mid-run leaves a row that says so rather than a row that
says `running` forever.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.jobs import store


@pytest.fixture
def engine(tmp_path: Path) -> Path:
    return tmp_path


def _submit(engine: Path, request: str = "add oauth2", project: str = "/repo") -> int:
    return store.submit(engine, project=project, request=request).id


# --- submission is durable before it is acknowledged ---


def test_submit_persists_the_job_and_returns_an_id(engine: Path) -> None:
    job_id = _submit(engine)

    job = store.get(engine, job_id)
    assert job.state == store.QUEUED
    assert job.request == "add oauth2"
    assert job.submitted_at
    assert job.run_id is None  # nothing has executed


def test_submit_writes_to_disk_so_another_process_can_read_it(engine: Path) -> None:
    """The whole point of the durable lifecycle: the caller may be gone by the
    time anything runs. A fresh connection stands in for a fresh process."""
    job_id = _submit(engine)

    assert (engine / store.DB_PATH).is_file()
    with store.connect(engine) as con:
        row = con.execute("SELECT request, state FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert (row["request"], row["state"]) == ("add oauth2", store.QUEUED)


def test_the_request_envelope_round_trips(engine: Path) -> None:
    job_id = store.submit(
        engine,
        project="/repo",
        request="add oauth2",
        channel="whatsapp",
        submitted_by="anass",
        envelope={"session_id": "s1", "dirty_policy": "reject"},
    ).id

    job = store.get(engine, job_id)
    assert job.channel == "whatsapp"
    assert job.submitted_by == "anass"
    assert job.envelope == {"session_id": "s1", "dirty_policy": "reject"}


# --- transitions: validated, timestamped, idempotent ---


def test_a_transition_is_recorded_with_a_timestamp(engine: Path) -> None:
    job_id = _submit(engine)

    assert store.transition(engine, job_id, store.RUNNING) is True

    job = store.get(engine, job_id)
    assert job.state == store.RUNNING
    assert job.started_at and job.heartbeat_at
    assert [e["to_state"] for e in store.events(engine, job_id)] == [store.QUEUED, store.RUNNING]


def test_repeating_a_transition_is_a_no_op(engine: Path) -> None:
    """A worker that crashed after writing `succeeded` but before acknowledging
    has to be able to say it again without corrupting the trail."""
    job_id = _submit(engine)
    store.transition(engine, job_id, store.RUNNING)
    store.transition(engine, job_id, store.SUCCEEDED)
    before = store.get(engine, job_id)

    assert store.transition(engine, job_id, store.SUCCEEDED) is False

    after = store.get(engine, job_id)
    assert after.finished_at == before.finished_at
    assert len(store.events(engine, job_id)) == 3  # no duplicate event


def test_an_illegal_transition_raises_rather_than_being_recorded(engine: Path) -> None:
    """`succeeded -> running` is a caller bug. Accepting it would make the
    audit trail describe something that never happened."""
    job_id = _submit(engine)
    store.transition(engine, job_id, store.RUNNING)
    store.transition(engine, job_id, store.SUCCEEDED)

    with pytest.raises(store.JobError, match="cannot go from succeeded to running"):
        store.transition(engine, job_id, store.RUNNING)

    assert store.get(engine, job_id).state == store.SUCCEEDED


def test_terminal_states_are_absorbing_except_interrupted(engine: Path) -> None:
    """Interrupted is the one finished state with work left to do: its branch
    and merged stages are still there, so it can be reopened — to `queued` and
    nowhere else, and only by `resume`, never by a worker on its own."""
    for terminal in sorted(store.TERMINAL_STATES - {store.INTERRUPTED}):
        assert store.TRANSITIONS[terminal] == frozenset()

    assert store.TRANSITIONS[store.INTERRUPTED] == frozenset({store.QUEUED})


def test_an_unknown_state_is_refused(engine: Path) -> None:
    with pytest.raises(store.JobError, match="Unknown job state"):
        store.transition(engine, _submit(engine), "vibing")


def test_a_transition_can_carry_progress_atomically(engine: Path) -> None:
    """State and the facts that explain it land in one transaction: a job that
    says `running` without saying what it is running is a row nobody can act
    on after a crash."""
    job_id = _submit(engine)

    store.transition(engine, job_id, store.RUNNING, run_id=7, branch="engine/x")

    job = store.get(engine, job_id)
    assert (job.state, job.run_id, job.branch) == (store.RUNNING, 7, "engine/x")


def test_a_field_that_is_not_progress_is_refused(engine: Path) -> None:
    """Column names come from the supervisor's keyword arguments, so an
    unexpected one has to fail loudly rather than write somewhere unintended."""
    with pytest.raises(store.JobError, match="Not job progress fields"):
        store.record_progress(engine, _submit(engine), state="succeeded")


# --- claiming: exactly one worker owns a job ---


def test_claiming_a_queued_job_marks_it_running_with_its_worker(engine: Path) -> None:
    job_id = _submit(engine)

    job = store.claim(engine, job_id, worker_pid=4242)

    assert job is not None
    assert job.state == store.RUNNING
    assert job.worker_pid == 4242
    assert job.worker_host


def test_a_second_claim_on_the_same_job_returns_nothing(engine: Path) -> None:
    """The guard lives in the UPDATE, not in a read-then-write, so two workers
    racing resolve in SQLite rather than both passing a check."""
    job_id = _submit(engine)
    store.claim(engine, job_id, worker_pid=1)

    assert store.claim(engine, job_id, worker_pid=2) is None
    assert store.get(engine, job_id).worker_pid == 1


def test_claim_next_takes_the_oldest_queued_job(engine: Path) -> None:
    first = _submit(engine, "first")
    second = _submit(engine, "second")

    assert store.claim_next(engine, worker_pid=1).id == first
    assert store.claim_next(engine, worker_pid=1).id == second
    assert store.claim_next(engine, worker_pid=1) is None


def test_claim_next_can_be_scoped_to_one_project(engine: Path) -> None:
    _submit(engine, "other repo", project="/other")
    mine = _submit(engine, "my repo", project="/mine")

    assert store.claim_next(engine, worker_pid=1, project="/mine").id == mine


def test_a_cancelled_job_cannot_be_claimed(engine: Path) -> None:
    job_id = _submit(engine)
    store.cancel(engine, job_id)

    assert store.claim(engine, job_id, worker_pid=1) is None


# --- heartbeat and crash recovery ---


def _age_heartbeat(engine: Path, job_id: int, *, seconds: float) -> None:
    stale = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    with store.connect(engine) as con:
        con.execute("UPDATE jobs SET heartbeat_at = ? WHERE id = ?", (stale, job_id))


def test_heartbeat_keeps_a_running_job_fresh(engine: Path) -> None:
    job_id = _submit(engine)
    store.claim(engine, job_id, worker_pid=1)
    _age_heartbeat(engine, job_id, seconds=600)
    assert store.get(engine, job_id).is_stale()

    store.heartbeat(engine, job_id)

    assert not store.get(engine, job_id).is_stale()


def test_a_heartbeat_cannot_revive_a_finished_job(engine: Path) -> None:
    job_id = _submit(engine)
    store.claim(engine, job_id, worker_pid=1)
    store.transition(engine, job_id, store.SUCCEEDED)

    store.heartbeat(engine, job_id)

    assert store.get(engine, job_id).heartbeat_at is None


def test_a_queued_job_is_never_stale(engine: Path) -> None:
    """Staleness is about a worker that stopped reporting. A queued job has no
    worker to hear from, however long it has been waiting."""
    assert not store.get(engine, _submit(engine)).is_stale()


def test_reconcile_marks_an_abandoned_run_interrupted(engine: Path) -> None:
    """A WSL restart or a killed terminal leaves a row saying `running`
    forever, and a queue whose "running" doesn't mean running is worse than no
    queue."""
    job_id = _submit(engine)
    store.claim(engine, job_id, worker_pid=1)
    store.record_progress(engine, job_id, branch="engine/x", integration_root="/tmp/engine-run-x")
    _age_heartbeat(engine, job_id, seconds=store.STALE_AFTER_SECONDS + 60)

    changed = store.reconcile(engine)

    assert [j.id for j in changed] == [job_id]
    job = store.get(engine, job_id)
    assert job.state == store.INTERRUPTED
    # not lost: where its work is, is still on the row
    assert job.branch == "engine/x"
    assert job.integration_root == "/tmp/engine-run-x"
    assert "no heartbeat" in store.events(engine, job_id)[-1]["note"]


def test_reconcile_leaves_a_live_run_alone(engine: Path) -> None:
    job_id = _submit(engine)
    store.claim(engine, job_id, worker_pid=1)

    assert store.reconcile(engine) == []
    assert store.get(engine, job_id).state == store.RUNNING


def test_reconcile_is_idempotent(engine: Path) -> None:
    job_id = _submit(engine)
    store.claim(engine, job_id, worker_pid=1)
    _age_heartbeat(engine, job_id, seconds=store.STALE_AFTER_SECONDS + 60)
    store.reconcile(engine)

    assert store.reconcile(engine) == []


def test_a_terminal_result_survives_a_restart(engine: Path) -> None:
    """Nothing is cached in the submitting process: a fresh read of the file
    is the only source of truth."""
    job_id = _submit(engine)
    store.claim(engine, job_id, worker_pid=1)
    store.transition(engine, job_id, store.SUCCEEDED, note="done", branch="engine/x")

    with store.connect(engine) as con:
        row = con.execute(
            "SELECT state, branch, finished_at FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()

    assert row["state"] == store.SUCCEEDED
    assert row["branch"] == "engine/x"
    assert row["finished_at"]


# --- cancellation ---


def test_cancel_stops_a_queued_job(engine: Path) -> None:
    job_id = _submit(engine)

    assert store.cancel(engine, job_id) is True
    assert store.get(engine, job_id).state == store.CANCELLED


def test_cancel_refuses_a_running_job_rather_than_pretending(engine: Path) -> None:
    """Nothing can stop a run mid-DAG yet (issue #29). Marking the row
    `cancelled` while provider calls kept spending quota would be a lie the
    queue tells about itself."""
    job_id = _submit(engine)
    store.claim(engine, job_id, worker_pid=1)

    with pytest.raises(store.JobError, match="cannot be stopped mid-run"):
        store.cancel(engine, job_id)

    assert store.get(engine, job_id).state == store.RUNNING


def test_cancel_reports_nothing_to_do_for_a_finished_job(engine: Path) -> None:
    job_id = _submit(engine)
    store.claim(engine, job_id, worker_pid=1)
    store.transition(engine, job_id, store.SUCCEEDED)

    assert store.cancel(engine, job_id) is False


# --- listing and retention ---


def test_recent_filters_by_state_and_project(engine: Path) -> None:
    _submit(engine, "a", project="/one")
    b = _submit(engine, "b", project="/two")
    store.cancel(engine, b)

    assert [j.request for j in store.recent(engine, state=store.QUEUED)] == ["a"]
    assert [j.request for j in store.recent(engine, project="/two")] == ["b"]


def test_purge_drops_old_terminal_jobs_but_never_active_ones(engine: Path) -> None:
    old = _submit(engine, "old")
    store.claim(engine, old, worker_pid=1)
    store.transition(engine, old, store.SUCCEEDED)
    long_ago = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    with store.connect(engine) as con:
        con.execute("UPDATE jobs SET finished_at = ? WHERE id = ?", (long_ago, old))
    still_queued = _submit(engine, "queued forever")
    with store.connect(engine) as con:
        con.execute("UPDATE jobs SET submitted_at = ? WHERE id = ?", (long_ago, still_queued))

    assert store.purge_older_than(engine, days=30) == 1

    assert [j.id for j in store.recent(engine)] == [still_queued]
    assert store.events(engine, old) == []


# --- resuming an interrupted job (the other half of crash recovery) ---


def _interrupt(engine: Path, **progress) -> int:
    job_id = _submit(engine)
    store.transition(engine, job_id, store.RUNNING, **progress)
    store.transition(engine, job_id, store.INTERRUPTED, note="worker presumed gone")
    return job_id


def test_resume_puts_an_interrupted_job_back_in_the_queue(engine: Path) -> None:
    job_id = _interrupt(engine, branch="engine/add-oauth2")

    assert store.resume(engine, job_id) is True

    assert store.get(engine, job_id).state == store.QUEUED


def test_resume_keeps_the_job_rather_than_starting_a_new_one(engine: Path) -> None:
    """Its request, its target, its branch and the worktree holding its merged
    stages are all still the ones being worked on. A fresh submission would
    abandon every part of that while calling itself a retry."""
    job_id = _interrupt(engine, branch="engine/add-oauth2", integration_root="/tmp/wt")

    store.resume(engine, job_id)

    job = store.get(engine, job_id)
    assert job.id == job_id
    assert job.request == "add oauth2"
    assert job.branch == "engine/add-oauth2"
    assert job.integration_root == "/tmp/wt"


def test_a_resumed_job_no_longer_claims_to_have_finished(engine: Path) -> None:
    """Leaving `finished_at` and `worker_pid` behind would show a job that is
    queued and waiting as one that already ended, owned by a dead pid."""
    job_id = _submit(engine)
    store.claim(engine, job_id, worker_pid=4242)
    store.transition(engine, job_id, store.INTERRUPTED)
    assert store.get(engine, job_id).finished_at

    store.resume(engine, job_id)

    job = store.get(engine, job_id)
    assert job.finished_at is None
    assert job.worker_pid is None


def test_resume_refuses_a_job_that_ran_to_a_verdict(engine: Path) -> None:
    """A failed run reached a conclusion; re-queueing it would re-run a
    workflow that has already been judged."""
    job_id = _submit(engine)
    store.transition(engine, job_id, store.RUNNING)
    store.transition(engine, job_id, store.FAILED)

    with pytest.raises(store.JobError, match="not interrupted"):
        store.resume(engine, job_id)

    assert store.get(engine, job_id).state == store.FAILED


def test_resume_refuses_a_queued_job(engine: Path) -> None:
    with pytest.raises(store.JobError, match="not interrupted"):
        store.resume(engine, _submit(engine))


def test_a_resumed_job_keeps_its_whole_history(engine: Path) -> None:
    job_id = _interrupt(engine, branch="engine/add-oauth2")

    store.resume(engine, job_id)
    store.claim(engine, job_id, worker_pid=1)

    states = [event["to_state"] for event in store.events(engine, job_id)]
    assert states == [
        store.QUEUED,
        store.RUNNING,
        store.INTERRUPTED,
        store.QUEUED,
        store.RUNNING,
    ]


def test_an_interrupted_job_cannot_be_reopened_into_anything_but_the_queue(
    engine: Path,
) -> None:
    """Nothing reopens it on its own — reconciliation only ever moves jobs
    *into* interrupted, and `resume` is the single way back out."""
    job_id = _interrupt(engine)

    with pytest.raises(store.JobError, match="cannot go from interrupted to succeeded"):
        store.transition(engine, job_id, store.SUCCEEDED)


# --- idempotency: a redelivery is not a second run (issue #26) ---


def _deliver(engine: Path, *, request="add oauth2", project="/repo", key="k1", payload=None):
    return store.submit(
        engine,
        project=project,
        request=request,
        channel="whatsapp",
        principal="whatsapp:+33600000000",
        idempotency_key=key,
        payload_hash=payload if payload is not None else f"hash-of-{request}-{project}",
    )


def test_a_first_delivery_creates_a_job(engine: Path) -> None:
    submission = _deliver(engine)

    assert submission.created is True
    assert store.get(engine, submission.id).state == store.QUEUED


def test_a_redelivery_returns_the_original_job_and_starts_nothing(engine: Path) -> None:
    """One phone message must not become two runs, two branches and twice the
    tokens — and the second would be invisible, since nothing about it looks
    different from outside."""
    first = _deliver(engine)

    second = _deliver(engine)

    assert second.id == first.id
    assert second.created is False
    assert len(store.recent(engine)) == 1
    assert len(store.events(engine, first.id)) == 1  # no second submission event


def test_a_redelivery_after_the_run_finished_still_returns_the_original(engine: Path) -> None:
    """A late retry of an already-completed message must not restart the work."""
    first = _deliver(engine)
    store.transition(engine, first.id, store.RUNNING)
    store.transition(engine, first.id, store.SUCCEEDED)

    assert _deliver(engine).id == first.id
    assert store.get(engine, first.id).state == store.SUCCEEDED


def test_idempotency_survives_a_restart(engine: Path) -> None:
    """The constraint is on disk, not in a process's memory — a fresh
    connection stands in for a fresh process."""
    first = _deliver(engine)

    with store.connect(engine) as con:
        con.execute("SELECT 1")  # a brand-new connection, as after a restart

    assert _deliver(engine).id == first.id


def test_a_different_message_starts_its_own_job(engine: Path) -> None:
    first = _deliver(engine, key="k1")
    second = _deliver(engine, key="k2", request="something else")

    assert second.id != first.id
    assert second.created is True


def test_the_same_key_with_different_content_is_refused(engine: Path) -> None:
    """Not a retry: a client reusing message ids, or a body swapped under an
    identifier that was already authorized. Honouring either version is wrong,
    so neither runs."""
    _deliver(engine, request="add oauth2")

    with pytest.raises(store.ReplayConflict):
        _deliver(engine, request="rm -rf everything")

    assert len(store.recent(engine)) == 1
    assert store.recent(engine)[0].request == "add oauth2"


def test_a_refused_conflict_is_audited_against_the_job_whose_key_was_reused(
    engine: Path,
) -> None:
    """The audit has to survive the exception that reports it — `connect` only
    commits on a clean exit, so a naive implementation records nothing exactly
    when something worth recording happened."""
    first = _deliver(engine, request="add oauth2")

    with pytest.raises(store.ReplayConflict):
        _deliver(engine, request="rm -rf everything")

    notes = [event["note"] for event in store.events(engine, first.id)]
    assert any("reusing this idempotency key" in note for note in notes)


def test_a_conflict_refusal_does_not_expose_the_stored_request(engine: Path) -> None:
    """Its request and target are prior run data, and whoever is presenting a
    mismatched payload has not established a right to read them."""
    _deliver(engine, request="deploy the secret thing", project="/private/repo")

    with pytest.raises(store.ReplayConflict) as caught:
        _deliver(engine, request="something else")

    assert "secret" not in str(caught.value)
    assert "/private/repo" not in str(caught.value)


def test_submissions_with_no_key_never_collide(engine: Path) -> None:
    """SQLite treats every NULL as distinct but not every '', so the partial
    index has to exclude the empty key explicitly — otherwise the second
    keyless submission collides with the first."""
    first = store.submit(engine, project="/repo", request="one")
    second = store.submit(engine, project="/repo", request="two")

    assert first.created and second.created
    assert first.id != second.id


def test_the_principal_is_recorded_on_the_job(engine: Path) -> None:
    job_id = _deliver(engine).id

    assert store.get(engine, job_id).principal == "whatsapp:+33600000000"


def test_the_submission_event_names_who_submitted(engine: Path) -> None:
    job_id = _deliver(engine).id

    assert "whatsapp:+33600000000" in store.events(engine, job_id)[0]["note"]


def test_a_pre_existing_database_gains_the_new_columns(engine: Path) -> None:
    """`CREATE TABLE IF NOT EXISTS` cannot retrofit a column onto a database
    that already exists, and this one is live on the developer's machine."""
    import sqlite3

    con = sqlite3.connect(engine / store.DB_PATH)
    con.executescript(
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY, state TEXT NOT NULL,"
        " project TEXT NOT NULL, request TEXT NOT NULL, channel TEXT NOT NULL DEFAULT 'cli',"
        " submitted_by TEXT NOT NULL DEFAULT '', envelope TEXT NOT NULL DEFAULT '{}',"
        " run_id INTEGER, base_ref TEXT NOT NULL DEFAULT '', base_sha TEXT NOT NULL DEFAULT '',"
        " branch TEXT NOT NULL DEFAULT '', integration_root TEXT NOT NULL DEFAULT '',"
        " stage TEXT NOT NULL DEFAULT '', attempt INTEGER NOT NULL DEFAULT 0,"
        " worker_pid INTEGER, worker_host TEXT NOT NULL DEFAULT '',"
        " submitted_at TEXT NOT NULL, started_at TEXT, heartbeat_at TEXT, finished_at TEXT,"
        " summary TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '');"
        "INSERT INTO jobs(state, project, request, submitted_at)"
        " VALUES('succeeded', '/repo', 'an older run', '2026-01-01T00:00:00+00:00');"
    )
    con.commit()
    con.close()

    old = store.recent(engine)[0]

    assert old.request == "an older run"
    # keyless, so it neither collides with other pre-existing rows nor claims a
    # delivery identity nothing ever established
    assert old.idempotency_key == ""
    assert _deliver(engine).created is True
