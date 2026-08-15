# ADR-012: Remote control loop and constrained local models as the MVP boundary

- Status: Proposed
- Date: 2026-08-11

## Context

The local engine now covers the hard execution primitives: context selection, explicit provider/model/effort routing, isolated worktrees, validation, telemetry, durable jobs, project admission, token/call reservations and scoped approvals. The product goal is to continue engineering from a phone through OpenClaw and validate a committed change in a browser.

A local queue is not a mobile product surface. Exposing its CLI or worker would leave authentication, progress, cancellation, Git delivery, previews and secret retention implicit. At the same time, the product should demonstrate provider sovereignty in the first usable loop, but without making a local runtime or an untested model a silent safety or quality bypass.

## Decision

Define the first expanded release as a **remote control loop with a constrained local-provider tier**, not a general-purpose agent platform:

`authenticated message -> REST/SSE typed operation -> durable job/events -> provider policy -> isolated execution -> approval -> committed delivery revision -> authenticated ephemeral preview -> human validation`

OpenClaw owns channel interaction and notifications. AI Platform remains authoritative for project admission, context, provider/model/effort policy, local-model eligibility, budgets, worktrees, validation, artifacts and audit. The public contract is a small set of typed operations: submit, status, events, cancel, approve/deny and fetch-artifact.

The MVP exit gates are:

1. authenticated principal, project allowlist and replay-safe idempotency;
2. REST commands plus structured lifecycle events and cooperative cancellation;
3. explicit provider/model/effort routing for Claude/Codex and a measured local provider tier;
4. synchronized, pinned Git base and explicit remote delivery policy;
5. hard token/call/time/currency ceilings, secret redaction/retention and auditable approvals;
6. CI/CD preview from the committed delivery revision, with authentication, expiry and teardown;
7. managed worker health and compact mobile result views.

Local models are MVP-eligible only for explicitly allowed low-risk roles (documentation, formatting, summaries and bounded fixes after policy permits them). Architecture, security decisions, migrations, secrets and external actions remain ineligible by default. The policy and evaluation gate is tracked by [#48](https://github.com/4nass/ai-platform/issues/48), while adapter plumbing remains [#37](https://github.com/4nass/ai-platform/issues/37).

The full issue mapping and ordering live in [MVP trajectory](../mvp-trajectory.md). A local building block can be marked engine-delivered while its remote gate remains open.

## Consequences

The product has a concrete finish line that can be tested end-to-end from a phone and demonstrates a sovereign provider option without hiding quality or safety trade-offs. Security and delivery responsibilities are explicit, and OpenClaw cannot become an accidental unrestricted shell.

The engine-side foundations for authenticated transport, durable events, service supervision, Git delivery guards and provider-neutral previews are now implemented and tested. The MVP still requires production CI/provider integration, concrete gateway deployment, complete artifact/secrets retention evidence and an evaluation suite for local models. Rich attachments, dynamic workflow composition, adaptive quality routing and cross-machine locking remain later features.

## Alternatives

- **Expose the existing CLI through OpenClaw:** rejected because it has no authenticated typed contract and would conflate interaction with execution policy.
- **Build a browser UI first:** rejected because notifications and conversational submission still need a gateway contract, while a preview browser is only one validation artifact.
- **Call the project complete after the local queue:** rejected because durable local state does not provide a remote principal, delivery revision or preview URL.
- **Make every role eligible for local models immediately:** rejected; the MVP includes a constrained provider tier with explicit capabilities and evaluation, not a silent downgrade of architecture or security work.
