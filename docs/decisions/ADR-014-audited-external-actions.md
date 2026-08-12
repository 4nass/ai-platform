# ADR-014: One audited executor for external actions

- Status: Accepted
- Date: 2026-08-12
- Scope: push, pull-request, preview and future consequential integrations

## Decision

All consequential external actions pass through one durable executor. Inputs are
typed action plans, project policy is resolved before execution, and the
existing approval store remains the only approval mechanism. The executor
persists an immutable fingerprint and a request id, consumes approvals against
the exact current inputs, invokes an injected handler, and appends provider and
cleanup outcomes to an audit log.

Git push has a built-in non-force handler that revalidates the issue #33 remote
base and the current delivery commit. Pull-request and preview integrations
supply handlers later; they do not create parallel policy or credential paths.

## Consequences

A failed action is not retried from the same request id. A caller must make an
explicit new request and, when policy requires it, receive a new approval.
Credentials are opaque project-scoped handler inputs and never part of the plan,
database record, audit payload or mobile response. The executor is intentionally
not a shell runner and cannot accept arbitrary commands or caller-controlled
repository paths.
