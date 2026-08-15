# ADR-017: Fail-closed remote security readiness gate

- Status: Accepted
- Date: 2026-08-15
- Issue: #49

## Context

The REST/SSE transport and OpenClaw adapter make remote control possible, but a reachable endpoint is not sufficient evidence that the engineering engine is safe to expose. Authentication, project admission, approvals, budgets, sandboxing, secret handling and auditability must be evaluated together. A partial deployment must not look like a supported remote MVP.

## Decision

Ship a deterministic ai-platform security-check command and a reusable core.security_readiness report. Every blocking dependency produces PASS or FAIL evidence. The server also refuses non-loopback binds unless remote enablement, TLS termination and rate limiting are explicitly configured.

The default decision is NO_GO. A time-bounded, owner-signed JSON risk acceptance may produce RISK_ACCEPTED for an operator, but the report keeps remote_ready=false and shows every failed control. This is an exception workflow, not a bypass.

## Consequences

- CI and operators have a machine-readable v1 readiness artifact.
- The local-only default remains safe and the emergency disable switch is documented.
- The gate intentionally reports NO_GO while #35 and #45, host sandbox prerequisites and production network evidence are incomplete.
- The check validates deterministic primitives; release review must still exercise secret injection through all log, event, artifact and notification sinks.

## Alternatives rejected

- Enabling the API whenever credentials exist: credentials do not prove budgets, sandbox or secret retention.
- A warning-only health check: warnings are routinely ignored during remote rollout.
- Letting the model decide readiness: trust-boundary policy must remain outside model output.
