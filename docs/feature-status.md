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
| `ai-platform doctor` preflight diagnostics | Delivered | PASS/WARN/FAIL checks for engine, providers and target validation |
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
| Hard budgets with reservations | Engine delivered | Token/call gate is real; time/currency ceilings remain in #45 |
| Scoped approvals for privileged actions | Engine delivered | Fingerprint-bound, single-use approvals plus shared audited action executor (#46) |
| Durable jobs, detached worker and crash recovery | Delivered | Heartbeat and reconciliation mark abandoned runs `interrupted` |
| Resuming an interrupted run | Delivered | `ai-platform resume <id>` skips merged stages |
| Structured progress events and cooperative cancellation | Engine delivered | Durable lifecycle events, cursor replay and cooperative cancellation are implemented; gateway consumption remains #30/#47 |
| REST/SSE remote API | Engine delivered | Authenticated WSGI REST/SSE server, replay, cancellation, approvals and artifact reads are implemented; production exposure remains #49 |
| Constrained local-model provider tier | Planned | Adapter direction is #37; MVP role policy/evaluation is #48 |
| Dirty-tree snapshot mode | Known limitation | Default `head` policy excludes uncommitted changes |
| Cross-machine run locking | Known limitation | Current lock is intra-machine only |
| Truly read-only target checkout | Known limitation | Target-local context artifacts can still be written |

## Remote personal gateway roadmap

| Priority | Capability | Issue | Status |
|---|---|---:|---|
| P0 | Safe ephemeral-write policy | [#23](https://github.com/4nass/ai-platform/issues/23) | Delivered; issue closed |
| P0 | Durable asynchronous lifecycle | [#24](https://github.com/4nass/ai-platform/issues/24) | Delivered; issue closed |
| P0 | Project registry and allowlist | [#25](https://github.com/4nass/ai-platform/issues/25) | Engine delivered; issue closed |
| P0 | Authentication, authorization and idempotency | [#26](https://github.com/4nass/ai-platform/issues/26) | Engine delivered: signed transport verifier, durable replay ledger and authenticated API consumption; production exposure remains #49 |
| P0 | Hard admission budgets | [#27](https://github.com/4nass/ai-platform/issues/27) | Token/call reservations delivered; time/currency ceilings are #45 |
| P0 | Approval gates for privileged actions | [#28](https://github.com/4nass/ai-platform/issues/28) | Gate and shared audited executor delivered; concrete PR/preview integrations remain |
| P1 | Structured events and cancellation | [#29](https://github.com/4nass/ai-platform/issues/29) | Engine delivered: durable events, cursor replay and cooperative cancellation; gateway integration remains |
| P1 | OpenClaw tool/API integration | [#30](https://github.com/4nass/ai-platform/issues/30) | Engine delivered: versioned typed async tools and restart-safe contract; concrete gateway wiring/TLS remains #47/#49 |
| P1 | Provider failover hardening | [#31](https://github.com/4nass/ai-platform/issues/31) | Planned |
| P1 | Quality-aware routing | [#32](https://github.com/4nass/ai-platform/issues/32) | Planned |
| P1 | Base synchronization and remote delivery | [#33](https://github.com/4nass/ai-platform/issues/33) | Engine delivered: pinned base, fetch/divergence policy and approval-only push guard; end-to-end PR delivery remains #30/#46/#47 |
| P1 | Per-run preview environments | [#34](https://github.com/4nass/ai-platform/issues/34) | Engine delivered: immutable provider contract, capability URLs, TTL/reconcile/cleanup, REST status/artifact links; concrete CI provider remains |
| P1 | Secrets isolation and retention | [#35](https://github.com/4nass/ai-platform/issues/35) | Planned; required by #49 |
| P1 | Multi-turn run references | [#36](https://github.com/4nass/ai-platform/issues/36) | Planned |
| P1 | Local-model provider | [#37](https://github.com/4nass/ai-platform/issues/37) | Planned; MVP policy/evaluation is #48 |
| P2 | Attachments and rich artifacts | [#38](https://github.com/4nass/ai-platform/issues/38) | Planned |
| P2 | Incremental context indexing | [#39](https://github.com/4nass/ai-platform/issues/39) | Planned |
| P2 | Managed local user service | [#40](https://github.com/4nass/ai-platform/issues/40) | Delivered in engine with Linux/systemd, WSL2/systemd and macOS/launchd profiles; host enablement remains operator-specific |
| P2 | Configuration consolidation | [#41](https://github.com/4nass/ai-platform/issues/41) | Delivered; issue closed |
| P2 | Notification channels | [#42](https://github.com/4nass/ai-platform/issues/42) | Engine delivered: channel-neutral compact rendering, preferences, redaction and idempotent retryable outbox; concrete gateway adapters remain |
| P1 | REST/SSE remote API | [#47](https://github.com/4nass/ai-platform/issues/47) | Engine delivered: authenticated WSGI API and SSE replay; TLS/rate-limit deployment remains in #49 |
| P1 | Local-model MVP policy and evaluation | [#48](https://github.com/4nass/ai-platform/issues/48) | Planned; constrained roles only |
| P0 | Remote exposure security readiness gate | [#49](https://github.com/4nass/ai-platform/issues/49) | Engine delivered: preflight `security-check` with `ATTESTED` for controls this process cannot observe, no override, and gate revalidation before any non-loopback bind; decision remains NO_GO until #35, host sandbox, production credentials and TLS/rate-limit attestations are in place |

## MVP gate

The first expanded phone-usable release is defined in [MVP trajectory](mvp-trajectory.md). The engine-side foundations are delivered in #29/#30/#33/#34/#40/#42/#44/#46/#47. Release readiness still requires constrained local-model execution (#37/#48), complete secrets/retention (#35), time/currency ceilings (#45), concrete provider/gateway deployment and the security readiness gate (#49).

Local engine delivery of #25-#28 is a prerequisite, not a substitute for the remote gates. Local models are included in this MVP only for explicitly allowed low-risk roles and only after the evaluation policy in #48 passes.

## Tracking reconciliation

GitHub keeps the parent safety issues open when their production or remote halves are unfinished. Transport auth (#44), structured events (#29), REST/SSE (#47), OpenClaw tools (#30), base synchronization (#33), previews (#34), service/notifications (#40/#42) and the audited executor (#46) now have engine implementations. Remaining MVP scope is #35, #37/#48, #45 and #49 plus concrete gateway/provider deployment. Close a parent only when its full end-to-end contract is delivered.

## Release rule

A feature moves to **Delivered** only when its public behavior, failure states, configuration, and tests are merged. Experimental or uncommitted code is **Planned**, even if it works locally. Update this page, the component document, and an ADR when the architectural contract changes.
