# Feature status and roadmap

This page is the authoritative distinction between implemented behavior and target architecture.

## Status vocabulary

- **Delivered**: implemented on the main product path and covered by automated tests.
- **In progress**: code may exist locally or on a branch, but the capability is not yet a stable contract.
- **Planned**: tracked design work with no delivered end-to-end capability.
- **Known limitation**: intentional boundary or defect that materially changes expected behavior.

## Engineering engine

| Capability | Status | Notes |
|---|---|---|
| CLI run, context, route, quota, and history commands | Delivered | Synchronous local interface |
| Semantic, graph, Git-diff, and memory context | Delivered | Target-local index and graph |
| Fixed prunable workflow DAG | Delivered | Decomposer selects a subset and complexity |
| Claude Code and Codex CLI execution | Delivered | Subscription-backed local sessions |
| Explicit provider/model/effort profiles | Delivered | Role and complexity policy |
| Quota- and health-aware failover | Delivered | Advisory local usage and recent outcomes |
| Integration, stage, and validation worktrees | Delivered | Delivery branch retained; checkout not switched |
| Frozen target validation policy | Delivered | Read from the base revision |
| Sandboxed tests with Bubblewrap when available | Delivered | Falls back with a warning when unavailable |
| Strict ignored-write policy | Delivered | Declared ephemeral paths are allowed |
| Bounded review/correction loop | Delivered | Only eligible validation/review failures |
| SQLite telemetry and cost estimates | Delivered | Analytical history, not a hard budget |
| Durable jobs, detached worker, and crash recovery | Delivered | `core/jobs/`; heartbeat + reconciliation mark abandoned runs `interrupted`, not `failed` |
| Dirty-tree snapshot mode | Known limitation | Declared policy is not fully implemented |
| Cross-machine run locking | Known limitation | Current lock is intra-machine only |
| Truly read-only target checkout | Known limitation | Target-local context artifacts may still be written |

## Remote personal gateway roadmap

| Priority | Capability | Issue | Status |
|---|---|---:|---|
| P0 | Safe ephemeral-write policy | [#23](https://github.com/4nass/ai-platform/issues/23) | Delivered; issue closed |
| P0 | Durable asynchronous lifecycle | [#24](https://github.com/4nass/ai-platform/issues/24) | Delivered in code; issue remains open |
| P0 | Project registry and allowlist | [#25](https://github.com/4nass/ai-platform/issues/25) | Planned |
| P0 | Authentication, authorization, idempotency | [#26](https://github.com/4nass/ai-platform/issues/26) | Planned |
| P0 | Hard admission budgets | [#27](https://github.com/4nass/ai-platform/issues/27) | Planned |
| P0 | Approval gates for privileged actions | [#28](https://github.com/4nass/ai-platform/issues/28) | Planned |
| P1 | Structured events and cancellation | [#29](https://github.com/4nass/ai-platform/issues/29) | Planned |
| P1 | OpenClaw tool/API integration | [#30](https://github.com/4nass/ai-platform/issues/30) | Planned |
| P1 | Provider failover hardening | [#31](https://github.com/4nass/ai-platform/issues/31) | Planned |
| P1 | Quality-aware routing | [#32](https://github.com/4nass/ai-platform/issues/32) | Planned |
| P1 | Base synchronization and remote delivery | [#33](https://github.com/4nass/ai-platform/issues/33) | Planned |
| P1 | Per-run preview environments | [#34](https://github.com/4nass/ai-platform/issues/34) | Planned |
| P1 | Secrets isolation and retention | [#35](https://github.com/4nass/ai-platform/issues/35) | Planned |
| P1 | Multi-turn run references | [#36](https://github.com/4nass/ai-platform/issues/36) | Planned |
| P1 | Local-model provider | [#37](https://github.com/4nass/ai-platform/issues/37) | Planned |
| P2 | Attachments and rich artifacts | [#38](https://github.com/4nass/ai-platform/issues/38) | Planned |
| P2 | Incremental context indexing | [#39](https://github.com/4nass/ai-platform/issues/39) | Planned |
| P2 | Reliable WSL service mode | [#40](https://github.com/4nass/ai-platform/issues/40) | Planned |
| P2 | Configuration consolidation | [#41](https://github.com/4nass/ai-platform/issues/41) | Delivered; issue remains open |
| P2 | Notification channels | [#42](https://github.com/4nass/ai-platform/issues/42) | Planned |

## Release rule

A feature moves to **Delivered** only when its public behavior, failure states, configuration, and tests are merged. Experimental or uncommitted code is **In progress**, even if it works locally. Update this page, the component document, and an ADR when the architectural contract changes.
