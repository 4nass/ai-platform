# Feature status and roadmap

This page is the authoritative distinction between implemented behavior and target architecture. GitHub issues track delivery; this page explains whether a capability is usable on the local engine or end-to-end through the remote product.

## Status vocabulary

- **Delivered:** implemented on the main product path and covered by automated tests.
- **Engine delivered:** deterministic local building block exists, but the authenticated remote contract is not complete.
- **Planned:** tracked design work with no delivered end-to-end capability.
- **Known limitation:** intentional boundary or defect that materially changes expected behavior.

## Engineering engine

| Capability | Status | Notes |
|---|---|---|
| CLI run, context, route, quota and history commands | Delivered | Synchronous local interface |
| Semantic, graph, Git-diff and memory context | Delivered | Target-local index and graph |
| Fixed prunable workflow DAG | Delivered | Decomposer selects a bounded subset and complexity |
| Claude Code and Codex CLI execution | Delivered | Subscription-backed local sessions |
| Explicit provider/model/effort profiles | Delivered | Role and complexity policy |
| Quota- and health-aware failover | Delivered | Advisory local usage and recent outcomes |
| Integration, stage and validation worktrees | Delivered | Delivery branch retained; checkout not switched |
| Frozen target validation policy | Delivered | Read from the base revision |
| Project registry and action allowlist | Delivered | `--project <id>`; canonicalized and re-checked per job |
| Idempotent, replay-safe submission | Delivered | Keyed on transport ids; conflicting redelivery refused |
| Sandboxed tests with Bubblewrap when available | Delivered | Explicit warning fallback when unavailable |
| Strict ignored-write policy | Delivered | Declared ephemeral paths are allowed |
| Bounded review/correction loop | Delivered | Only eligible validation/review failures |
| SQLite telemetry and cost estimates | Delivered | Analytical history, not a financial ceiling |
| Hard budgets with reservations | Engine delivered | Token/call gate is real; time/currency ceilings remain |
| Scoped approvals for privileged actions | Engine delivered | Fingerprint-bound and audited; external actions remain |
| Durable jobs, detached worker and crash recovery | Delivered | Heartbeat and reconciliation mark abandoned runs `interrupted` |
| Resuming an interrupted run | Delivered | `ai-platform resume <id>` skips merged stages |
| Structured progress events and cooperative cancellation | Planned | Local job state exists; remote event/cancel contract is #29 |
| Dirty-tree snapshot mode | Known limitation | Default `head` policy excludes uncommitted changes |
| Cross-machine run locking | Known limitation | Current lock is intra-machine only |
| Truly read-only target checkout | Known limitation | Target-local context artifacts can still be written |

## Remote personal gateway roadmap

| Priority | Capability | Issue | Status |
|---|---|---:|---|
| P0 | Safe ephemeral-write policy | [#23](https://github.com/4nass/ai-platform/issues/23) | Delivered; issue closed |
| P0 | Durable asynchronous lifecycle | [#24](https://github.com/4nass/ai-platform/issues/24) | Delivered; issue closed |
| P0 | Project registry and allowlist | [#25](https://github.com/4nass/ai-platform/issues/25) | Engine delivered; issue closed |
| P0 | Authentication, authorization and idempotency | [#26](https://github.com/4nass/ai-platform/issues/26) | Engine half delivered; authenticated transport remains in #30 |
| P0 | Hard admission budgets | [#27](https://github.com/4nass/ai-platform/issues/27) | Token/call reservations delivered; time/currency ceilings remain |
| P0 | Approval gates for privileged actions | [#28](https://github.com/4nass/ai-platform/issues/28) | Gate delivered; external push/deploy actions remain in #33/#34 |
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
| P2 | Configuration consolidation | [#41](https://github.com/4nass/ai-platform/issues/41) | Delivered; issue closed |
| P2 | Notification channels | [#42](https://github.com/4nass/ai-platform/issues/42) | Planned |

## MVP gate

The first phone-usable release is defined in [MVP trajectory](mvp-trajectory.md). It requires the remote admission/lifecycle contract (#29/#30), synchronized delivery and immutable artifacts (#33), authenticated previews (#34), secrets/retention (#35), and a reliable worker/notification path (#40/#42). Local engine delivery of #25-#28 is a prerequisite, not a substitute for those remote gates.

## Tracking reconciliation

GitHub still lists #26-#28 as open because their remote halves are intentionally unfinished. Do not close those issues merely because the engine-side modules are merged; close or split them only when the corresponding end-to-end transport/action contract is delivered.

## Release rule

A feature moves to **Delivered** only when its public behavior, failure states, configuration, and tests are merged. Experimental or uncommitted code is **Planned**, even if it works locally. Update this page, the component document, and an ADR when the architectural contract changes.
