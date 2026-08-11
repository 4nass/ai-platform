# ADR-006: OpenClaw as interaction gateway

- Status: Accepted
- Date: 2026-08-01

## Context

The owner wants to submit work, follow tests, and validate results from a phone while away from the workstation. OpenClaw can unify Signal, WhatsApp, Telegram, and conversational interaction, but it is not an engineering execution sandbox or durable workflow engine.

## Decision

Use OpenClaw only as an optional, replaceable personal interaction adapter. It translates Signal, WhatsApp, Telegram or another channel into the stable [REST/SSE API contract](../api-contract.md). A browser UI, CLI or another adapter must be able to use the same contract without changing the engine.

The API exposes only narrow authenticated and idempotent platform operations: submit a job, read status/events, cancel, approve a privileged transition, and fetch artifact or preview references.

Keep project policy, provider routing, budgets, worktrees, validation, and job state inside AI Platform. Do not give OpenClaw unrestricted shell access or duplicate engineering business logic in the adapter.

## Consequences

Messaging channels stay decoupled from engineering semantics and can retry safely. OpenClaw can be removed or replaced without changing runs, jobs, providers or Git delivery. The platform must deliver the P0 remote-readiness prerequisites before enabling any adapter. Gateway compromise is contained by project allowlists, scoped operations, budgets, and approvals.

## Alternatives

- **OpenClaw directly runs Codex/Claude commands:** rejected because state, authorization, and audit become fragmented.
- **Build every messaging connector in AI Platform:** rejected because interaction-channel integration is not the core product.
- **Browser-only UI:** valid as another adapter and possibly sufficient for the first MVP; it does not remove the need for a stable API contract.
