# Structured run events and cooperative cancellation

Issue #29 adds a durable event stream to the job store. The event id is the
monotonic cursor; consumers request `events_page(job_id, after=cursor, limit=N)`
(or `events_since`) and can resume without replaying an acknowledged event. Each
event has version, event_type, job_id, optional run_id, stage_id, attempt, a UTC
timestamp, and a JSON payload.

Stable event types are `run.queued`, `run.started`, `context.selected`,
`provider.selected`, `stage.started`, `stage.completed`, `approval.required`,
`tests.completed`, `review.completed`, `run.completed`, `run.failed`,
`run.cancel_requested` and `run.cancelled`. State transitions remain the source of
truth; event writes are transactional with the transition.

## Cancellation is a request, then a stop

Those are two moments, and the row distinguishes them.

`cancel` on a **queued** job cancels it: nothing is running, so there is nothing
to wait for. `cancel` on a **running** job moves it to `cancel_requested` — its worker
still has a provider subprocess to signal and worktrees to remove, and a row
claiming `cancelled` before any of that happened would be asserting a stop that
has not occurred, with quota still being spent behind it. Only the worker that
actually unwound moves the job to `cancelled`. A run that had already finished
by the time it noticed resolves to the outcome it really reached, not to
`cancelled`.

`cancellation_requested()` is what a worker's watcher asks (`cancel_requested` or
`cancelled`); `is_cancelled()` is whether it has actually stopped. The request
is idempotent.

## Unwinding, not failing

`CancellationRequested` is a `BaseException` for the same reason
`KeyboardInterrupt` and `asyncio.CancelledError` are: it is an instruction to
unwind, not a failure some layer might handle and continue past. As an
`Exception` it was caught by the broad handlers that turn a stage's problems
into a failed `StageResult`, so cancelling mid-stage reported
`failed: CancellationRequested` — the request recorded as an error in the work
it was cancelling.

CLI providers run in dedicated process groups: cancellation sends a graceful
termination signal, waits up to two seconds, then force-kills the group. An
in-flight `anthropic_api` request is not interruptible and completes before the
unwind continues.

## Nothing is left behind

Cancellation can unwind from any of several points — dispatch, verify, review,
each correction attempt — so cleanup is attached to the `with` block that owns
the run's target-level state rather than written at one of them. The integration
worktree is removed wherever the unwind starts, and a cancelled stage removes
its own task worktree: unlike a crash, a cancellation is a decision to abandon
that work. The delivery branch and the job's event history remain for diagnosis.
