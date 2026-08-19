# Audited external action executor

Issue #46 provides the single policy boundary for consequential actions. Callers
construct one of the typed plans in core.actions.executor:

- GitPushPlan: immutable branch/commit/base/remote, non-force push;
- OpenPRPlan: branch, commit, base branch and hashed title/body;
- PreviewDeployPlan: service, environment, commit, configuration digest and
  bounded TTL.

No plan accepts an arbitrary shell command, repository path from a remote
payload, or credential. The project is resolved through the registry before
the executor is called.

## Lifecycle

The executor persists one execution keyed by a caller-supplied request_id.
Reusing the same id with the same fingerprint returns the existing result;
reusing it with changed inputs is rejected. The unique database constraint
decides concurrent submissions; a losing caller returns the winning execution
rather than an SQLite error. State changes are conditional on the prior state,
so concurrent transitions cannot overwrite each other. A failed action is
never retried implicitly.

Policy is evaluated as automatic, denied or approval_required. Required
actions create the existing fingerprint-bound, single-use, expiring approval.
When a caller supplies the approved id, approvals.consume verifies the exact
action, target, commit, command metadata and amount before the handler runs.
Changing any of those inputs invalidates the approval.

Each request, refusal, approval consumption, provider result, failure, cleanup
result and cancellation is appended to action_events. Only a bounded provider summary and provider identifier are persisted; credentials
and unbounded provider output are never stored or returned. Public execution and
event reads require the creating principal and return the same not-found result
for an unknown or foreign execution id, preventing cross-principal enumeration.

## Handlers and deployment

GitPushHandler verifies the delivery branch commit and the recorded remote base
before invoking the non-force push guard from issue #33. PR and preview handlers
are injected by their integrations (#33/#34), so provider credentials remain
outside this generic module. A credential provider may supply an opaque
project-scoped credential to a handler; it is never serialized by the executor.

Cancellation state is durable, but the in-flight signal is currently only available
to the in-process handler through cancel_event. A separate process cannot stop
an already-running provider call yet; the executor records cancel_requested
and never starts a second call automatically.

This branch delivers a library boundary, not an end-to-end action surface:
no CLI command, worker stage or REST route instantiates the executor today.
It must be wired through one principal-aware entry point before it can be
called a delivered push, pull-request or preview feature.

The action database is in jobs.sqlite with the job coordination data and uses
the queue's WAL, foreign-key and owner-only connection policy. Backups and
retention must treat action audit events as security-sensitive records.
