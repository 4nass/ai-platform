# ADR-012: Remote control loop as the MVP boundary

- Status: Proposed
- Date: 2026-08-11

## Context

The local engine now covers the hard execution primitives: context selection, explicit provider/model/effort routing, isolated worktrees, validation, telemetry, durable jobs, project admission, token/call reservations and scoped approvals. The product goal, however, is to continue engineering from a phone through OpenClaw and validate a committed change in a browser.

A local queue is not a mobile product surface. Exposing its CLI or worker would leave authentication, progress, cancellation, Git delivery, previews and secret retention implicit. Conversely, implementing local models, rich attachments or a dynamic planner before the remote loop would add capability without making the product usable away from the workstation.

## Decision

Define the first usable release as a **remote control loop**, not a general-purpose agent platform:

`authenticated message -> typed operation -> durable job/events -> isolated execution -> approval -> committed delivery revision -> authenticated ephemeral preview -> human validation`

OpenClaw owns channel interaction and notifications. AI Platform remains authoritative for project admission, context, provider/model/effort policy, budgets, worktrees, validation, artifacts and audit. The public contract is a small set of typed operations: submit, status, events, cancel, approve/deny and fetch-artifact.

The MVP exit gates are:

1. authenticated principal, project allowlist and replay-safe idempotency;
2. structured lifecycle events and cooperative cancellation;
3. synchronized, pinned Git base and explicit remote delivery policy;
4. hard token/call/time/currency ceilings, secret redaction/retention and auditable approvals;
5. CI/CD preview from the committed delivery revision, with authentication, expiry and teardown;
6. managed worker health and compact mobile result views.

The full issue mapping and ordering live in [MVP trajectory](../mvp-trajectory.md). A local building block can be marked engine-delivered while its remote gate remains open.

## Consequences

The product has a concrete finish line that can be tested end-to-end from a phone. Security and delivery responsibilities are explicit, and OpenClaw cannot become an accidental unrestricted shell. Some attractive features are intentionally later: local-model execution, attachments, dynamic workflow composition, adaptive quality routing and cross-machine locking.

The MVP still requires CI/provider integration, authenticated transport, artifact retention, service supervision and a browser-facing deployment path. Until those gates are complete, the documented local-owner boundary remains the only supported operating mode.

## Alternatives

- **Expose the existing CLI through OpenClaw:** rejected because it has no authenticated typed contract and would conflate interaction with execution policy.
- **Build a browser UI first:** rejected because notifications and conversational submission still need a gateway contract, while a preview browser is only one validation artifact.
- **Call the project complete after the local queue:** rejected because durable local state does not provide a remote principal, delivery revision or preview URL.
- **Make local models or dynamic planning MVP blockers:** rejected; they improve cost and breadth but do not close the first phone-driven loop.
