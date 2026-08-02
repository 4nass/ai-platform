# Architecture decision records

ADRs preserve durable technical decisions, their context, and trade-offs. Component documents describe what exists; ADRs explain why a boundary was chosen.

## Status

| ADR | Decision | Status |
|---|---|---|
| [ADR-001](ADR-001-local-first-single-user.md) | Local-first, single-user engine | Accepted |
| [ADR-002](ADR-002-git-worktree-isolation.md) | Git worktrees and delivery branches | Accepted |
| [ADR-003](ADR-003-snapshot-context-and-frozen-policy.md) | Snapshot-consistent context and frozen target policy | Accepted |
| [ADR-004](ADR-004-explicit-model-effort-routing.md) | Explicit model and effort routing | Accepted |
| [ADR-005](ADR-005-separate-telemetry-and-job-stores.md) | Separate SQLite telemetry and job stores | Accepted |
| [ADR-006](ADR-006-openclaw-as-gateway.md) | OpenClaw as interaction gateway | Proposed |
| [ADR-007](ADR-007-preview-environments.md) | Immutable per-run preview environments | Proposed |
| [ADR-008](ADR-008-platform-config-and-presets.md) | Two-tier configuration — platform.yaml plus internal presets | Accepted |
| [ADR-009](ADR-009-resumable-runs-and-git-owned-checkpoints.md) | Resumable runs, checkpointed in git rather than in the queue | Accepted |
| [ADR-010](ADR-010-project-registry-as-the-admission-boundary.md) | A project registry, not a path, is the admission boundary | Accepted |
| [ADR-011](ADR-011-admission-authorization-and-approval.md) | Admission, authorization and approval as one layered boundary | Accepted |

The older CLI dry-run decision remains in [memory/adr/ADR-001](../../memory/adr/ADR-001-cli-dry-run-flag.md). It predates this documentation structure and is retained for history.

## ADR lifecycle

- **Proposed**: design direction, not a delivered contract.
- **Accepted**: implemented and relied upon.
- **Superseded**: replaced by a newer ADR; keep the original and link both.
- **Rejected**: considered but intentionally not adopted.

Create an ADR when changing a trust boundary, storage ownership, execution isolation, provider selection authority, external integration contract, or delivery model. Include context, decision, consequences, and alternatives. Do not edit an accepted decision to hide historical trade-offs; supersede it.
