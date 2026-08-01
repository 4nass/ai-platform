# ADR-006: OpenClaw as interaction gateway

- Status: Proposed
- Date: 2026-08-01

## Context

The owner wants to submit work, follow tests, and validate results from a phone while away from the workstation. OpenClaw can unify Signal, WhatsApp, Telegram, and conversational interaction, but it is not an engineering execution sandbox or durable workflow engine.

## Decision

Use OpenClaw only as the personal interaction gateway. Expose narrow authenticated and idempotent platform operations: submit a job, read status/events, cancel, approve a privileged transition, and fetch artifact or preview references.

Keep project policy, provider routing, budgets, worktrees, validation, and job state inside AI Platform. Do not give the gateway unrestricted shell access.

## Consequences

Messaging channels stay decoupled from engineering semantics and can retry safely. The platform must deliver the P0 remote-readiness prerequisites before enabling the integration. Gateway compromise is contained by project allowlists, scoped operations, budgets, and approvals.

## Alternatives

- **OpenClaw directly runs Codex/Claude commands:** rejected because state, authorization, and audit become fragmented.
- **Build every messaging connector in AI Platform:** rejected because interaction-channel integration is not the core product.
- **Browser-only UI:** useful for previews and approval, but insufficient as the only notification/conversation channel.
