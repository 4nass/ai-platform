# ADR-004: Explicit model and effort routing

- Status: Accepted
- Date: 2026-08-01

## Context

Provider-only preferences do not express model capability or reasoning effort. Letting each agent choose its own model makes cost, availability, compatibility, failover, and audits unpredictable. A single premium default also consumes scarce subscription quota on routine work.

## Decision

The engine owns ordered `provider + model + effort` profiles for every role. The decomposer may choose only a bounded run complexity: `routine`, `complex`, or `critical`. Configuration maps role and complexity to profiles. The router may skip a profile only for measurable quota or health gates.

If every candidate is gated, the first declared profile runs and the reason is recorded. The second provider is fallback, not a consensus call.

## Consequences

Architecture, security, review, and critical correction can receive stronger reasoning while routine tests and documentation use economical profiles. Both Claude and Codex paths remain explicit and testable. Model identifiers and CLI effort support must be maintained over time.

Quota pressure remains advisory for routing; hard token/call admission budgets are now a separate delivered engine gate, while time/currency ceilings remain planned.

## Alternatives

- **Agent chooses model:** rejected because it delegates platform governance to untrusted execution.
- **One model for every role:** rejected because it wastes quota and ignores task risk.
- **Dynamic quality optimization immediately:** deferred until telemetry has enough reliable samples.
