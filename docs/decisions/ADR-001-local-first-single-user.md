# ADR-001: Local-first, single-user engine

- Status: Accepted
- Date: 2026-08-01

## Context

The product starts as a personal engineering platform running on one developer workstation and reusing local Git repositories and authenticated provider subscriptions. Building a multi-tenant control plane first would add identity, tenancy, infrastructure, and compliance costs before the execution engine is proven.

## Decision

Keep the engineering engine local-first and single-user. The stable interface is the local CLI. Shared configuration and telemetry belong to the engine root; project artifacts belong to the target repository. Network and mobile access must be added through a separate authenticated gateway/API boundary.

## Consequences

Local dependencies such as Git, SQLite, Qdrant file mode, provider CLIs, `flock`, and Bubblewrap are appropriate. Multi-machine coordination, high availability, public API security, and multi-tenant isolation are outside the delivered boundary.

Remote access cannot simply expose the CLI. It requires durable jobs, allowlisted projects, authentication, idempotency, budgets, approvals, and secrets policy.

## Alternatives

- **Multi-tenant SaaS immediately:** rejected because it expands scope before the core workflow is validated.
- **Messaging bot directly invoking shell commands:** rejected because it collapses interaction, authorization, and execution trust boundaries.
