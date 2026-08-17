"""Executes queued jobs: claim one, run it, land a terminal state.

The half of issue #24 that does the work. `core.jobs.store` says what a job
is and what states it may be in; this decides when a job moves between them
and makes sure something is always writing the row while a run is in flight.

Two entry points, because two callers need different things:

- `run_job` executes one specific job in this process. `ai-platform submit`
  spawns it detached, so submission returns an id without waiting for a
  provider.
- `drain` claims and runs whatever is queued until nothing is. That is what a
  managed service (issue #40) would call, and what makes a job submitted while
  no worker was running get picked up later rather than sitting forever.

Both go through the same claim/heartbeat/finalize path, so there is one answer
to "what happens if this dies halfway" rather than one per entry point.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

from core.jobs import budget, store

HEARTBEAT_SECONDS = 20.0
"""How often a running job says it is still alive. Well under
`store.STALE_AFTER_SECONDS` so an ordinary hiccup — a slow provider call, a
loaded machine — never crosses the staleness line; the gap between them is
the tolerance, not the detection latency."""


class _CancellationWatcher:
    def __init__(self, engine_root: Path, job_id: int, event: threading.Event, interval: float = 0.2):
        self.engine_root, self.job_id, self.event, self.interval = engine_root, job_id, event, interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._watch, daemon=True, name=f"cancel-{job_id}")
    def _watch(self):
        while not self._stop.wait(self.interval):
            try:
                if store.cancellation_requested(self.engine_root, self.job_id):
                    self.event.set()
                    return
            except Exception:
                pass
    def __enter__(self):
        self._thread.start()
        return self
    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=self.interval * 2)

class _Heartbeat:
    """Keeps a job's `heartbeat_at` fresh for as long as the run lasts.

    A daemon thread rather than a heartbeat written between stages: a single
    provider call can take minutes, and a run is at its most alive exactly
    when it has nothing to report. Writing only at stage boundaries would make
    the longest, most normal part of a run look like a crash.

    Daemon so it can never keep the process up, and every write is
    best-effort: a database hiccup must not take down the run it is describing.
    """

    def __init__(self, engine_root: Path, job_id: int, *, interval: float | None = None):
        self.engine_root = engine_root
        self.job_id = job_id
        # Resolved here rather than as a default argument so the interval is
        # read at call time: a default is bound when the function is defined,
        # which would make HEARTBEAT_SECONDS unadjustable and the thread itself
        # untestable without waiting the full period.
        self.interval = HEARTBEAT_SECONDS if interval is None else interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._beat, daemon=True, name=f"heartbeat-{job_id}")

    def _beat(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                store.heartbeat(self.engine_root, self.job_id)
            except Exception:
                pass  # a missed beat is recoverable; a raised one kills the thread

    def __enter__(self) -> _Heartbeat:
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._stop.set()
        self._thread.join(timeout=self.interval)


def _is_repo_busy(exc: BaseException) -> bool:
    """Whether this failure is "another run holds the target", not "this run
    is broken".

    Matched on the message because `git_ops.exclusive_run_lock` raises a plain
    RuntimeError — narrow, and the alternative (a dedicated exception type)
    would put queue vocabulary into the git layer for one caller's benefit.
    Getting it wrong is not silent: a miss fails the job with the real message
    attached, which is visible in `ai-platform status`.
    """
    return isinstance(exc, RuntimeError) and "already modifying" in str(exc)


def run_job(engine_root: Path, job_id: int, *, claim: bool = True) -> str:
    """Runs one job to a terminal state and returns that state.

    `claim=False` is for a caller that already owns the job (see `drain`),
    which must not try to claim it a second time — the claim is what makes
    concurrent workers safe, so claiming twice would mean the guard never ran.
    """
    if claim:
        job = store.claim(engine_root, job_id, worker_pid=os.getpid())
        if job is None:
            # Someone else got there first, or it was cancelled between
            # submission and now. Both are normal; neither is this worker's
            # to override.
            return store.get(engine_root, job_id).state
    else:
        job = store.get(engine_root, job_id)

    from core.orchestrator import supervisor

    def progress(**fields) -> None:
        store.record_progress(engine_root, job_id, **fields)

    def emit(event_type, **kwargs) -> None:
        try:
            store.emit_event(engine_root, job_id, event_type, **kwargs)
        except Exception:
            pass

    cancel_event = threading.Event()
    try:
        target, admitted = _admitted_target(engine_root, job)
        with _Heartbeat(engine_root, job_id), _CancellationWatcher(engine_root, job_id, cancel_event):
            report = supervisor.run(
                engine_root,
                target,
                job.request,
                session_id=job.envelope.get("session_id"),
                dirty_policy=job.envelope.get("dirty_policy", supervisor.DIRTY_HEAD),
                progress=progress,
                resume=_resume_state(job),
                project=admitted,
                cancel_event=cancel_event,
                emit_event=emit,
            )
    except store.CancellationRequested:
        # Only now is `cancelled` true: the run has unwound, its provider
        # subprocess is gone and its worktrees are removed. Whoever asked moved
        # the row to `cancel_requested` and deliberately left this half undone.
        store.transition(
            engine_root, job_id, store.CANCELLED,
            note="cancelled mid-run", event_type="run.cancelled",
        )
        return store.CANCELLED
    except budget.BudgetExceeded as exc:
        # Paused, not failed. The run is well-formed and its work so far is on
        # a branch; what stopped it is a policy ceiling, and the answer is a
        # human decision (raise the limit, or let it go) rather than a retry.
        _request_budget_approval(engine_root, job, exc)
        store.transition(
            engine_root,
            job_id,
            store.WAITING_APPROVAL,
            note=f"budget: {exc.decision.reason}",
            payload={"reason": exc.decision.reason},
        )
        return store.WAITING_APPROVAL
    except BaseException as exc:
        if _is_repo_busy(exc):
            # Back to the queue, not failed: the job is fine, the target was
            # busy. Without this a queue would burn a submission every time
            # two runs overlapped on one repo.
            store.transition(
                engine_root, job_id, store.QUEUED, note=f"target repo busy: {exc}"
            )
            return store.QUEUED
        store.transition(
            engine_root,
            job_id,
            store.FAILED,
            note=f"{type(exc).__name__}: {exc}",
            payload={"error": type(exc).__name__},
        )
        raise

    if store.cancellation_requested(engine_root, job_id):
        # Asked to stop, but it had already finished — `cancel_requested` resolves to
        # the outcome the run actually reached rather than to `cancelled`.
        state = store.SUCCEEDED if report.summary == "done" else store.FAILED
        store.transition(
            engine_root, job_id, state,
            note=f"{report.summary} (cancellation arrived too late)",
            branch=report.branch, payload={"summary": report.summary, "cancelled_too_late": True},
        )
        _record_outcome(engine_root, job_id, report)
        return state
    state = store.SUCCEEDED if report.summary == "done" else store.FAILED
    store.transition(
        engine_root,
        job_id,
        state,
        note=report.summary,
        branch=report.branch,
        payload={"summary": report.summary},
    )
    _record_outcome(engine_root, job_id, report)
    return state


def _request_budget_approval(engine_root: Path, job: store.Job, exc) -> None:
    """Turns a budget refusal into something a person can act on.

    Without this the job would sit in `waiting_approval` with nothing to
    approve — a state that describes a wait nobody can end. The request carries
    the exact amount, so approving it authorizes *that* overrun and not a
    standing licence to exceed the budget (see core.jobs.approvals).

    Best-effort: the pause itself is the safety property, and failing to file
    the paperwork must not turn a paused run into a crashed one.
    """
    from core.jobs import approvals

    try:
        approvals.request(
            engine_root,
            action="budget",
            target=f"job-{job.id}",
            detail={
                "extra_tokens": max(0, exc.decision.would_total - exc.decision.ceiling),
                "limit": exc.decision.limit,
                "ceiling": exc.decision.ceiling,
            },
            job_id=job.id,
            run_key=f"run-{job.run_id}" if job.run_id else "",
            requested_by=job.principal,
        )
    except Exception:
        pass


def _admitted_target(engine_root: Path, job: store.Job):
    """Re-checks the allowlist at execution time, and returns (path, project).

    A job submitted by project id is *not* admitted once, at submission. A
    queue exists precisely so work can execute long after it was asked for —
    after the registry was edited, after a project was withdrawn, after a path
    was repointed. Trusting the path recorded on the row would make the
    allowlist a snapshot taken at the least useful moment, and a job queued
    before a project was removed would still reach it.

    The resolved path is also used in preference to the stored one, so a
    project that legitimately moved is followed rather than half-followed.

    A job submitted by raw path (`--repo`, local interactive use) has no
    project id and is run as-is: it was admitted by someone who could already
    reach that directory, which is a different trust context (see
    `ai_platform._admit`).
    """
    from core.orchestrator import registry

    project_id = job.envelope.get("project_id")
    if not project_id:
        return Path(job.project), None

    project = registry.resolve(engine_root, project_id, action=registry.MODIFY)
    return project.path, project


def _resume_state(job: store.Job):
    """The interrupted run this job should continue, if there is one.

    Read off the row and the disk rather than carried as a flag: a job only has
    an `integration_root` because an earlier attempt created one, so a queued
    job that already owns a worktree with a readable checkpoint *is* a resumed
    one by construction. A flag would be a second source of truth, and the one
    that can disagree with the disk is the one that decides whether real merged
    work gets abandoned.

    Answering None means "start over", which is right for every way this can
    come back empty: a job interrupted before it created a worktree, one whose
    worktree the user has since deleted, or a checkpoint truncated by the very
    crash being recovered from.
    """
    from core.orchestrator import checkpoint, supervisor

    if not job.integration_root or not job.branch:
        return None
    if checkpoint.load(Path(job.integration_root)) is None:
        return None
    # The branch comes from the row, not the checkpoint, so the supervisor
    # cross-checks two independent records before adding to a branch.
    return supervisor.Resume(
        branch=job.branch, integration_root=Path(job.integration_root)
    )


def _record_outcome(engine_root, job_id: int, report) -> None:
    """The one-line answer `ai-platform status` shows, stored on the job.

    Duplicated from the telemetry row on purpose: a job that never got far
    enough to have a `runs` row still has to be able to say what happened, and
    a status read shouldn't need to join two databases to produce one
    sentence.
    """
    stages = ", ".join(f"{s.id}:{s.status}" for s in report.stages)
    tests = "PASS" if report.tests_passed else "FAIL"
    review = {True: "PASS", False: "FAIL", None: "UNKNOWN"}[report.review_passed]
    detail = f"tests={tests} review={review}"
    if report.correction_attempts:
        detail += f" corrections={report.correction_attempts}"
    if stages:
        detail += f" | {stages}"
    with store.connect(engine_root) as con:
        con.execute(
            "UPDATE jobs SET summary = ?, detail = ? WHERE id = ?",
            (report.summary, detail, job_id),
        )


def reconcile(engine_root: Path) -> list:
    """Marks abandoned jobs interrupted, and repairs what they left behind.

    `store.reconcile` handles the row, `budget.reconcile` the capacity a dead
    run is still holding. The git repair is here because it touches a git
    repository, which the job store deliberately knows nothing about:
    a run neutralizes the target's `core.hooksPath` for its whole
    write-capable window and restores it in a `finally`, and a `finally` only
    runs if the process lives to run it. A killed worker therefore left the
    user's own git hooks silently disabled in their repository, indefinitely
    — confirmed on a real SIGKILL'd run. Reconciliation is the one place that
    learns a run died, so it is where that gets put back.
    """
    import git

    from core.orchestrator import git_ops

    # Reservations held by a run that is no longer spending would otherwise
    # shrink every later run's window forever — a budget that tightens itself
    # every time something crashes (see core.jobs.budget.reconcile).
    budget.reconcile(engine_root)

    interrupted = store.reconcile(engine_root)
    for job in interrupted:
        try:
            git_ops.restore_hooks(git.Repo(job.project))
        except Exception:
            # A target that has moved, been deleted, or was never a repo is
            # not a reason to leave the *other* interrupted jobs unreconciled.
            pass
    return interrupted


def drain(engine_root: Path, *, project: str | None = None, limit: int | None = None) -> list[int]:
    """Claims and runs queued jobs until there are none left.

    Stops on the first job that comes back `queued` (its target repo is busy):
    continuing would spin through the rest of the queue, hit the same lock and
    put every one of them back, burning the queue for no progress.
    """
    ran: list[int] = []
    while limit is None or len(ran) < limit:
        job = store.claim_next(engine_root, worker_pid=os.getpid(), project=project)
        if job is None:
            return ran
        ran.append(job.id)
        if run_job(engine_root, job.id, claim=False) == store.QUEUED:
            return ran
    return ran


def spawn_detached(engine_root: Path, job_id: int) -> int:
    """Starts a worker for one job in its own process and returns its pid.

    Detached from the submitting terminal: the caller's whole reason for
    submitting rather than running is that it intends to leave. `start_new_session`
    puts the worker in its own process group so closing the terminal (SIGHUP)
    doesn't take the run with it, and output goes to the job row rather than
    to a parent that may be gone — a run whose stdout is a closed pipe dies of
    SIGPIPE somewhere unhelpful.
    """
    return subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "ai_platform", "work", "--job", str(job_id)],
        cwd=str(engine_root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    ).pid
