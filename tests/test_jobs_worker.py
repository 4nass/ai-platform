"""Tests for core.jobs.worker — executing a submitted job (issue #24).

These drive real `supervisor.run` calls against a real git repo with fake
providers, rather than mocking the supervisor: what is under test is the seam
between the two — that a run's progress reaches the job row, and that every
way a run can end lands the job in the right terminal state.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import git
import pytest

from core.jobs import store, worker
from core.orchestrator import git_ops
from tests.test_supervisor import (
    _multi_stage_run,
    _patch_provider,
    _patch_tests,
    fake_repo,  # noqa: F401 - pytest fixture
)


def _queue(engine: Path, target: Path, request: str = "add oauth2", **envelope) -> int:
    return store.submit(
        engine, project=str(target), request=request, envelope=envelope or {}
    ).id


def test_run_job_executes_the_run_and_lands_succeeded(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")
    job_id = _queue(fake_repo, fake_repo)

    assert worker.run_job(fake_repo, job_id) == store.SUCCEEDED

    job = store.get(fake_repo, job_id)
    assert job.state == store.SUCCEEDED
    assert job.summary == "done"
    assert job.finished_at
    assert job.heartbeat_at is None  # nothing is running any more


def test_a_run_that_needs_attention_lands_failed(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    _patch_provider(monkeypatch, _multi_stage_run(verdict="VERDICT: FAIL"))
    _patch_tests(monkeypatch, passed=True, output="ok")
    job_id = _queue(fake_repo, fake_repo)

    assert worker.run_job(fake_repo, job_id) == store.FAILED
    assert store.get(fake_repo, job_id).summary == "needs attention"


def test_an_exception_lands_failed_with_the_reason_on_the_job(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """A crashed run must not leave a row saying `running` — the failure is
    the one thing the submitter will come back for."""

    def explode(*args, **kwargs):
        raise ValueError("planner exploded")

    monkeypatch.setattr("core.orchestrator.planner.plan", explode)
    job_id = _queue(fake_repo, fake_repo)

    with pytest.raises(ValueError):
        worker.run_job(fake_repo, job_id)

    job = store.get(fake_repo, job_id)
    assert job.state == store.FAILED
    assert "planner exploded" in store.events(fake_repo, job_id)[-1]["note"]


def test_a_busy_target_repo_returns_the_job_to_the_queue(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """One mutating run per repo is a scheduling constraint, not a defect in
    the job. Failing it would burn a submission every time two runs overlapped
    on one target."""
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")
    job_id = _queue(fake_repo, fake_repo)

    with git_ops.exclusive_run_lock(git.Repo(fake_repo)):
        assert worker.run_job(fake_repo, job_id) == store.QUEUED

    job = store.get(fake_repo, job_id)
    assert job.state == store.QUEUED
    assert "busy" in store.events(fake_repo, job_id)[-1]["note"]
    # and it really is runnable afterwards
    assert worker.run_job(fake_repo, job_id) == store.SUCCEEDED


def test_run_job_records_where_the_work_is_as_it_goes(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """The point of progress reporting: a job interrupted mid-run still says
    which commit it started from, which branch it produced and which run it
    was, so its work is recoverable instead of orphaned."""
    seen: list[dict] = []
    real_progress = store.record_progress

    def spy(engine_root, job_id, **fields):
        seen.append(dict(fields))
        real_progress(engine_root, job_id, **fields)

    monkeypatch.setattr(store, "record_progress", spy)
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")
    job_id = _queue(fake_repo, fake_repo)

    worker.run_job(fake_repo, job_id)

    recorded: dict = {}
    for fields in seen:
        recorded.update(fields)
    assert recorded["base_sha"] == git.Repo(fake_repo).head.commit.hexsha
    assert recorded["base_ref"] == "master"
    assert recorded["branch"].startswith("engine/")
    assert recorded["run_id"]
    # every DAG stage was reported while it was in flight
    stages = {s for fields in seen for s in fields.get("stage", "").split(", ") if s}
    assert {"architecture", "backend", "frontend", "verify", "review"} <= stages

    job = store.get(fake_repo, job_id)
    assert job.base_sha and job.branch and job.run_id
    # the worktree was removed on success, so the row must not still point at it
    assert job.integration_root == ""


def test_the_job_links_to_its_telemetry_run(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """The two databases stay separate; `run_id` is the only join they need."""
    from core.telemetry import store as telemetry

    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")
    job_id = _queue(fake_repo, fake_repo)

    worker.run_job(fake_repo, job_id)

    run_id = store.get(fake_repo, job_id).run_id
    with telemetry.connect(fake_repo) as con:
        row = con.execute("SELECT request, summary FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert (row["request"], row["summary"]) == ("add oauth2", "done")


def test_the_detail_line_summarises_the_outcome(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")
    job_id = _queue(fake_repo, fake_repo)

    worker.run_job(fake_repo, job_id)

    detail = store.get(fake_repo, job_id).detail
    assert "tests=PASS" in detail and "review=PASS" in detail
    assert "backend:done" in detail


def test_the_envelope_drives_the_run(monkeypatch: pytest.MonkeyPatch, fake_repo: Path) -> None:
    """Submission decides the policy; the worker only carries it. A dirty
    target with `reject` has to fail the job, not quietly run on HEAD."""
    Path(fake_repo, "dirty.txt").write_text("x", encoding="utf-8")
    _patch_provider(monkeypatch, _multi_stage_run())
    job_id = _queue(fake_repo, fake_repo, dirty_policy="reject")

    with pytest.raises(Exception, match="dirty-policy=reject"):
        worker.run_job(fake_repo, job_id)

    assert store.get(fake_repo, job_id).state == store.FAILED


def test_run_job_on_a_job_someone_else_claimed_does_nothing(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "core.orchestrator.planner.plan", lambda *a, **k: calls.append("ran") or []
    )
    job_id = _queue(fake_repo, fake_repo)
    store.claim(fake_repo, job_id, worker_pid=os.getpid() + 1)

    assert worker.run_job(fake_repo, job_id) == store.RUNNING
    assert calls == []


def test_drain_runs_every_queued_job(monkeypatch: pytest.MonkeyPatch, fake_repo: Path) -> None:
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")
    first = _queue(fake_repo, fake_repo, "first request")
    second = _queue(fake_repo, fake_repo, "second request")

    assert worker.drain(fake_repo) == [first, second]

    assert store.get(fake_repo, first).state == store.SUCCEEDED
    assert store.get(fake_repo, second).state == store.SUCCEEDED


def test_drain_on_an_empty_queue_is_a_no_op(fake_repo: Path) -> None:
    assert worker.drain(fake_repo) == []


def test_drain_stops_when_the_target_is_busy(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """Continuing would spin through the rest of the queue, hit the same lock
    and requeue every one of them for no progress."""
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")
    first = _queue(fake_repo, fake_repo, "first request")
    second = _queue(fake_repo, fake_repo, "second request")

    with git_ops.exclusive_run_lock(git.Repo(fake_repo)):
        assert worker.drain(fake_repo) == [first]

    assert store.get(fake_repo, first).state == store.QUEUED
    assert store.get(fake_repo, second).state == store.QUEUED


def test_the_heartbeat_thread_beats_while_a_stage_is_blocked(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """A single provider call can take minutes, and a run is at its most alive
    exactly when it has nothing to report. Beating only at stage boundaries
    would make the longest, most normal part of a run look like a crash.

    The stage ages the heartbeat to the year 2000 and then blocks: nothing
    inside the run touches it again, so anything that clears staleness had to
    come from the background thread.
    """
    monkeypatch.setattr(worker, "HEARTBEAT_SECONDS", 0.02)
    stale_at = "2000-01-01T00:00:00+00:00"
    beat_while_blocked: list[bool] = []
    inner = _multi_stage_run()

    def blocking_stage(task):
        if task.agent == "architect":
            job = store.recent(fake_repo, limit=1)[0]
            with store.connect(fake_repo) as con:
                con.execute("UPDATE jobs SET heartbeat_at = ? WHERE id = ?", (stale_at, job.id))
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if store.get(fake_repo, job.id).heartbeat_at != stale_at:
                    break
                time.sleep(0.02)
            beat_while_blocked.append(store.get(fake_repo, job.id).heartbeat_at != stale_at)
        return inner(task)

    _patch_provider(monkeypatch, blocking_stage)
    _patch_tests(monkeypatch, passed=True, output="ok")

    worker.run_job(fake_repo, _queue(fake_repo, fake_repo))

    assert beat_while_blocked == [True]


def test_a_worker_thread_never_outlives_the_run(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    import threading

    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")
    before = {t.name for t in threading.enumerate()}

    worker.run_job(fake_repo, _queue(fake_repo, fake_repo))

    assert not {t.name for t in threading.enumerate()} - before


def test_reconciling_an_abandoned_run_re_enables_the_targets_git_hooks(
    fake_repo: Path,
) -> None:
    """The defect a real SIGKILL'd run exposed: `disable_hooks` restores
    `core.hooksPath` in a `finally`, and a `finally` only runs if the process
    lives to run it. A killed worker left the user's own git hooks silently
    disabled in their repository, indefinitely. Reconciliation is the one
    place that learns a run died, so it is where that gets put back."""
    import shutil
    import tempfile
    from datetime import datetime, timedelta, timezone

    repo = git.Repo(fake_repo)
    neutral = tempfile.mkdtemp(prefix=git_ops.HOOKS_DISABLED_PREFIX)
    with repo.config_writer() as writer:
        writer.set_value(git_ops.SAVED_HOOKS_SECTION, git_ops.SAVED_HOOKS_OPTION, "<unset>")
        writer.set_value("core", "hooksPath", neutral)

    job_id = _queue(fake_repo, fake_repo)
    store.claim(fake_repo, job_id, worker_pid=1)
    stale = (
        datetime.now(timezone.utc) - timedelta(seconds=store.STALE_AFTER_SECONDS + 60)
    ).isoformat()
    with store.connect(fake_repo) as con:
        con.execute("UPDATE jobs SET heartbeat_at = ? WHERE id = ?", (stale, job_id))

    assert [j.id for j in worker.reconcile(fake_repo)] == [job_id]

    assert store.get(fake_repo, job_id).state == store.INTERRUPTED
    with pytest.raises(Exception):
        git.Repo(fake_repo).config_reader().get_value("core", "hooksPath")
    shutil.rmtree(neutral, ignore_errors=True)


def test_reconcile_survives_a_target_that_no_longer_exists(fake_repo: Path) -> None:
    """A target that has moved or been deleted is not a reason to leave the
    other interrupted jobs unreconciled."""
    from datetime import datetime, timedelta, timezone

    job_id = store.submit(fake_repo, project="/nonexistent/repo", request="x").id
    store.claim(fake_repo, job_id, worker_pid=1)
    stale = (
        datetime.now(timezone.utc) - timedelta(seconds=store.STALE_AFTER_SECONDS + 60)
    ).isoformat()
    with store.connect(fake_repo) as con:
        con.execute("UPDATE jobs SET heartbeat_at = ? WHERE id = ?", (stale, job_id))

    assert [j.id for j in worker.reconcile(fake_repo)] == [job_id]
    assert store.get(fake_repo, job_id).state == store.INTERRUPTED


# --- resuming an interrupted job (crash recovery's second half) ---


def _interrupted_job(monkeypatch: pytest.MonkeyPatch, fake_repo: Path) -> int:
    """A job left exactly as a killed worker leaves one.

    Assembled from the same pieces `run_job` uses — claim, run, record progress
    — but stopping short of a verdict, because that is the distinction: an
    interrupted job never reached one. Going through `run_job` instead would
    land it in `failed`, which is a run the engine completed and judged, and
    has nothing to resume.
    """
    from core.orchestrator import supervisor

    _patch_provider(monkeypatch, _multi_stage_run(fail_agents=frozenset({"tests"})))
    _patch_tests(monkeypatch, passed=True, output="ok")
    job_id = _queue(fake_repo, fake_repo)
    store.claim(fake_repo, job_id, worker_pid=os.getpid())
    supervisor.run(
        fake_repo,
        fake_repo,
        "add oauth2",
        progress=lambda **fields: store.record_progress(fake_repo, job_id, **fields),
    )
    store.transition(fake_repo, job_id, store.INTERRUPTED, note="worker presumed gone")
    return job_id


def test_a_resumed_job_continues_its_own_worktree_instead_of_a_new_one(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """The end-to-end property: a job that stops mid-DAG and is resumed lands
    its remaining stages on the branch it already had, keeping what it merged
    before the crash."""
    job_id = _interrupted_job(monkeypatch, fake_repo)

    interrupted = store.get(fake_repo, job_id)
    assert interrupted.branch and Path(interrupted.integration_root).exists()

    called: list[str] = []

    def fake_run(task):
        called.append(task.agent)
        return _multi_stage_run()(task)

    _patch_provider(monkeypatch, fake_run)
    store.resume(fake_repo, job_id)

    assert worker.run_job(fake_repo, job_id) == store.SUCCEEDED

    finished = store.get(fake_repo, job_id)
    assert finished.branch == interrupted.branch  # same deliverable, continued
    assert "architect" not in called  # already merged, not paid for twice
    assert "tests" in called


def test_a_job_with_no_worktree_yet_resumes_by_starting_over(fake_repo: Path) -> None:
    """Interrupted before it created anything — there is nothing to continue,
    and saying so beats refusing to run at all."""
    job_id = _queue(fake_repo, fake_repo)

    assert worker._resume_state(store.get(fake_repo, job_id)) is None


def test_a_job_whose_worktree_is_gone_resumes_by_starting_over(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    job_id = _interrupted_job(monkeypatch, fake_repo)

    job = store.get(fake_repo, job_id)
    git_ops.remove_worktree(git.Repo(fake_repo), Path(job.integration_root))

    assert worker._resume_state(store.get(fake_repo, job_id)) is None


def test_a_job_that_merged_stages_offers_them_back(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    job_id = _interrupted_job(monkeypatch, fake_repo)

    resume = worker._resume_state(store.get(fake_repo, job_id))

    assert resume is not None
    assert resume.branch == store.get(fake_repo, job_id).branch


# --- the allowlist is re-checked at execution time, not at submission (#25) ---


def _allowlist(engine: Path, target: Path, *, actions: str = "[inspect, modify, test]") -> None:
    (engine / "config").mkdir(exist_ok=True)
    (engine / "config" / "projects.yaml").write_text(
        f"roots: [{target.parent}]\nprojects:\n  mine:\n    path: {target}\n"
        f"    allowed_actions: {actions}\n",
        encoding="utf-8",
    )


def test_a_job_submitted_by_project_id_resolves_it_at_claim_time(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    _allowlist(fake_repo, fake_repo)
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")
    job_id = _queue(fake_repo, fake_repo, project_id="mine")

    assert worker.run_job(fake_repo, job_id) == store.SUCCEEDED


def test_a_project_withdrawn_after_submission_cannot_still_be_reached(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """A queue exists so work can execute long after it was asked for. If the
    allowlist were only consulted at submission, a job queued before a project
    was removed would still reach it — the check would be a snapshot taken at
    the least useful moment."""
    _allowlist(fake_repo, fake_repo)
    job_id = _queue(fake_repo, fake_repo, project_id="mine")

    (fake_repo / "config" / "projects.yaml").write_text("roots: []\nprojects: {}\n", encoding="utf-8")
    called: list = []
    monkeypatch.setattr("core.orchestrator.planner.plan", lambda *a, **k: called.append(1))

    with pytest.raises(Exception, match="No project 'mine'"):
        worker.run_job(fake_repo, job_id)

    assert called == []  # refused before any planning
    assert store.get(fake_repo, job_id).state == store.FAILED


def test_an_action_revoked_after_submission_stops_the_job(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    _allowlist(fake_repo, fake_repo)
    job_id = _queue(fake_repo, fake_repo, project_id="mine")

    _allowlist(fake_repo, fake_repo, actions="[inspect]")

    with pytest.raises(Exception, match="does not permit 'modify'"):
        worker.run_job(fake_repo, job_id)

    assert store.get(fake_repo, job_id).state == store.FAILED


def test_a_job_submitted_by_path_is_run_as_is(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """`--repo` is a different trust context: someone who could already `cd`
    there. The registry is not consulted, and an empty one does not block it."""
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")
    job_id = _queue(fake_repo, fake_repo)

    assert worker.run_job(fake_repo, job_id) == store.SUCCEEDED


# --- a hard budget pauses a job rather than failing it (issue #27) ---


def test_a_strict_budget_overrun_moves_the_job_to_waiting_approval(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """Paused, not failed. The run is well-formed and its work is on a branch;
    what stopped it is a policy ceiling, and the answer is a human decision
    rather than a retry."""
    platform = (fake_repo / "config" / "platform.yaml")
    platform.write_text(
        platform.read_text(encoding="utf-8")
        + "budgets:\n  mode: strict\n  classes:\n    standard: {max_run_tokens: 1}\n",
        encoding="utf-8",
    )
    _allowlist(fake_repo, fake_repo)
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")
    job_id = _queue(fake_repo, fake_repo, project_id="mine")

    assert worker.run_job(fake_repo, job_id) == store.WAITING_APPROVAL

    job = store.get(fake_repo, job_id)
    assert job.state == store.WAITING_APPROVAL
    assert "budget" in store.events(fake_repo, job_id)[-1]["note"]
    assert "max_run_tokens" in store.events(fake_repo, job_id)[-1]["note"]


def test_reconciliation_reclaims_budget_a_dead_run_still_holds(fake_repo: Path) -> None:
    from datetime import datetime, timedelta, timezone

    from core.jobs import budget

    budget.reserve(fake_repo, run_key="crashed", estimated=500_000)
    old = (datetime.now(timezone.utc) - timedelta(seconds=budget.STALE_AFTER_SECONDS + 60)).isoformat()
    with store.connect(fake_repo) as con:
        con.execute("UPDATE reservations SET created_at = ?", (old,))

    worker.reconcile(fake_repo)

    limits = budget.Limits(max_window_tokens=1_000_000)
    assert budget.usage(fake_repo, limits, run_key="crashed").window_tokens == 0


def test_a_paused_job_files_an_approval_someone_can_act_on(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """A job in `waiting_approval` with nothing to approve is a state that
    describes a wait nobody can end (issue #28)."""
    from core.jobs import approvals

    platform = fake_repo / "config" / "platform.yaml"
    platform.write_text(
        platform.read_text(encoding="utf-8")
        + "budgets:\n  mode: strict\n  classes:\n    standard: {max_run_tokens: 1}\n",
        encoding="utf-8",
    )
    _allowlist(fake_repo, fake_repo)
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")
    job_id = _queue(fake_repo, fake_repo, project_id="mine")

    worker.run_job(fake_repo, job_id)

    waiting = approvals.pending(fake_repo)
    assert len(waiting) == 1
    assert waiting[0].action == "budget"
    assert waiting[0].job_id == job_id
    assert waiting[0].detail["limit"] == "max_run_tokens"
    # the exact overrun, so approving authorizes that amount and not a standing
    # licence to exceed the budget
    assert waiting[0].detail["extra_tokens"] > 0


def test_filing_the_approval_cannot_turn_a_pause_into_a_crash(
    monkeypatch: pytest.MonkeyPatch, fake_repo: Path
) -> None:
    """The pause is the safety property; failing to file the paperwork must not
    cost it."""
    from core.jobs import approvals

    platform = fake_repo / "config" / "platform.yaml"
    platform.write_text(
        platform.read_text(encoding="utf-8")
        + "budgets:\n  mode: strict\n  classes:\n    standard: {max_run_tokens: 1}\n",
        encoding="utf-8",
    )
    _allowlist(fake_repo, fake_repo)
    _patch_provider(monkeypatch, _multi_stage_run())
    _patch_tests(monkeypatch, passed=True, output="ok")
    monkeypatch.setattr(
        approvals, "request", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down"))
    )
    job_id = _queue(fake_repo, fake_repo, project_id="mine")

    assert worker.run_job(fake_repo, job_id) == store.WAITING_APPROVAL
