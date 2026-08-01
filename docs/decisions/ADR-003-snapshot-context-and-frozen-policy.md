# ADR-003: Snapshot-consistent context and frozen target policy

- Status: Accepted
- Date: 2026-08-01

## Context

A dirty checkout can contain code absent from the run's committed base. Injecting its diff while agents modify a clean worktree gives them an inconsistent world. Likewise, allowing agents to edit validation configuration before it becomes effective lets model output weaken its own controls.

## Decision

Build run context from the integration worktree representing the run snapshot. Under the default dirty policy, warn about user changes but exclude them from context and execution. Read the effective `.ai-platform.yml` from the base revision before any agent runs and keep it fixed for the run.

Do not write a HEAD-keyed graph cache from dirty source state.

## Consequences

Agents see the code they can actually modify, and same-run changes cannot disable tests, sandboxing, timeouts, or ignored-write rules. Uncommitted user intent is not automatically included; users must commit it or eventually choose an explicit coherent snapshot mode.

## Alternatives

- **Inject dirty diff into a HEAD worktree:** rejected as internally inconsistent.
- **Require a clean tree always:** retained as an optional strict policy but too restrictive as the default personal workflow.
- **Trust the latest policy from the delivery branch:** rejected because an agent could weaken validation.
