# Structured run events and cooperative cancellation

Issue #29 adds a durable event stream to the job store. The event id is the
monotonic cursor; consumers request events_page(job_id, after=cursor, limit=N)
(or events_since) and can resume without replaying an acknowledged event. Each
event has version, event_type, job_id, optional run_id, stage_id, attempt, a UTC
timestamp, and a JSON payload.

Stable event types are run.queued, run.started, context.selected,
provider.selected, stage.started, stage.completed, approval.required,
tests.completed, review.completed, run.completed, run.failed, and run.cancelled.
State transitions remain the source of truth; event writes are transactional
with the transition.

Cancellation is idempotent for queued and running jobs. A worker watches the
durable state, propagates cancellation through the supervisor and scheduler,
and refuses late provider output. CLI providers run in dedicated process groups:
cancellation sends a graceful termination signal, waits up to two seconds, then
force-kills the group. Task and validation worktrees are removed on cancellation;
the job row and event history remain for diagnosis.
