# Remote security readiness gate

Issue #49 adds a deterministic barrier before exposing the REST/SSE API to a remote network. A working API is not sufficient evidence that the engineering engine is safe to expose: each trust boundary must produce local evidence.

## Command

    ai-platform security-check
    ai-platform security-check --json

The command returns 0 only for GO or RISK_ACCEPTED and returns 1 for NO_GO. JSON output is versioned as v1 for CI or release evidence. Secret values are never printed.

## Decisions

- GO: every blocking control is PASS.
- NO_GO: at least one blocking control is FAIL; keep the service local or disabled.
- RISK_ACCEPTED: an owner has recorded a temporary exception; remote_ready remains false and all failed checks remain visible.

WARN is informational and never bypasses a FAIL.

## Current implementation

The gate checks:

- transport credentials and required job scopes;
- allowlisted projects, canonical roots and action names;
- explicit remote enablement, non-loopback bind, TLS termination and rate limiting;
- strict budget mode and declared classes;
- the audited action executor and approval store;
- Bubblewrap plus committed target sandbox policy;
- redaction primitives and an explicit retention policy;
- REST/SSE route scopes and durable jobs, events and telemetry;
- a rollback/disable switch and a time-bounded risk-acceptance record.

The server accepts localhost binds. Any non-loopback bind requires explicit remote enablement, TLS termination and rate limiting.

## Evidence matrix

| Boundary | Evidence | Current state |
| --- | --- | --- |
| Identity and replay | HMAC principal, scopes, nonce ledger and idempotency | Engine delivered (#44) |
| Project admission | Registry id, canonical path and allowed actions | Delivered (#25) |
| API contract | Authenticated REST/SSE, status, events, cancel, approvals and artifacts | Engine delivered (#47) |
| Lifecycle | Durable events, cursors and cooperative cancellation | Engine delivered (#29) |
| OpenClaw | Typed submit/status/cancel/approve/diff/events adapter | Engine delivered (#30) |
| Git delivery | Base synchronization, divergence policy and approval-bound push | Engine delivered (#33/#46) |
| Preview | Immutable plan, capability URL, TTL and cleanup lifecycle | Engine delivered; concrete provider remains (#34) |
| Budgets | Token/call reservations | Delivered; time/currency ceilings remain (#45) |
| Secrets | Redaction and retention policy | Redaction primitives exist; complete policy/evidence remains (#35) |
| Sandbox | Bubblewrap and committed target policy | Host-dependent; required for remote readiness |
| Service/notifications | Managed local service and durable notification outbox | Engine delivered (#40/#42) |
| Production exposure | TLS, rate limiting, secret manager and gateway process | Not deployed; blocks remote MVP (#49) |

## Rollback and risk acceptance

Set AI_PLATFORM_REMOTE_ENABLED=false and restart the managed local user service to disable exposure. Credentials must come from the service secret manager, never from Git-tracked YAML.

A temporary exception is a local JSON file ignored by Git at config/security-risk-acceptance.json, or the path in AI_PLATFORM_RISK_ACCEPTANCE_FILE:

    {
      "id": "RA-49-001",
      "owner": "security-owner",
      "scope": "remote-mvp",
      "expires_at": "2026-09-01T00:00:00+00:00",
      "rationale": "Temporary exception with a scheduled review."
    }

A valid record changes the operator decision to RISK_ACCEPTED only. It never changes remote_ready to true.

## MVP status

The engine-side gate is implemented and tested. The repository remains NO_GO until #35 retention/redaction evidence, #45 time/currency enforcement, host sandbox prerequisites and production credentials/TLS/rate-limit evidence are complete.
