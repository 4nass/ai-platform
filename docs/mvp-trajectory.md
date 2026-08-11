# MVP objectives and trajectory

## Objective

Make the personal engineering loop usable away from the workstation without turning the project into a public SaaS:

```text
phone / messenger
        -> OpenClaw
        -> authenticated typed platform operation
        -> durable job and structured events
        -> isolated run, tests and review
        -> approval when required
        -> pushed delivery revision
        -> ephemeral authenticated preview
        -> phone notification and human validation
```

The platform remains the authority for project admission, context, provider/model/effort routing, token budgets, worktrees, validation, artifacts and audit. OpenClaw is only the interaction gateway.

## What is already usable

The local CLI can run synchronous work or submit a durable job. It can select context, route Claude Code/Codex CLI profiles, isolate each run in Git worktrees, validate in a disposable checkout, review/correct within a bound, record telemetry, enforce token/call budgets, gate scoped approvals, recover a crashed worker and resume merged stages. These capabilities are covered by the automated suite and are safe inside the documented single-user workstation boundary.

## MVP exit criteria

| Gate | Exit criterion | Tracking |
|---|---|---|
| Admission | A remote caller authenticates as a principal; only an allowlisted project id and authorized operation are accepted; retries are idempotent | Engine half delivered in #25/#26; transport in #30 |
| Lifecycle | Submit, status, progress events, cancellation, approval and artifact references work without a live terminal | #29, #30 |
| Execution | The run starts from a synchronized, pinned base revision and produces a remote delivery branch without mutating the user's checkout | #33 |
| Safety | Token/call/time/currency ceilings, secret redaction/retention, fail-closed sandbox policy and auditable approvals are enforced | #27, #28, #35; time/currency and external actions remain |
| Validation | A successful delivery revision is deployed by CI/CD to an authenticated, ephemeral preview URL with expiry and teardown | #34 |
| Operability | The worker starts as a managed WSL service, survives restart, exposes health/backup status and emits a compact result view/notification | #40, #42 |

The MVP is complete only when every gate has an end-to-end test through the remote contract. A local implementation of a gate is not the same thing as a reachable mobile product surface.

## Ordered roadmap

### Now - remote control loop (P0/P1)

1. **#30 - typed authenticated OpenClaw tools:** submit, status, events, cancel, approve/deny and fetch-artifact. Keep the API narrow and idempotent.
2. **#29 - structured progress and cooperative cancellation:** make stage, provider, budget, validation, review and approval transitions observable.
3. **#33 - Git synchronization and delivery:** pin the base ref, define divergence policy, push the delivery branch and record immutable commit/artifact ids.
4. **#34 - preview environments:** let CI/CD build the committed delivery revision, deploy a short-lived authenticated subdomain and tear it down deterministically.
5. **#35 - secrets and retention:** redact logs/transcripts, scope credentials per project, encrypt where needed, and implement deletion/retention checks.
6. **#40/#42 - service and notifications:** keep one worker healthy under WSL and return compact mobile-friendly result views.

### Next - reliability and quality (P1/P2)

- #31 live provider failover, retry classification and circuit breakers;
- #32 quality-aware routing based on accepted outcomes, not process success alone;
- #36 deterministic multi-turn references for projects, runs, diffs and approvals;
- #21 CI, automated PR checks and exercised security analysis;
- #22 package `core/` and `providers/` into the built wheel;
- #14 review usefulness/factual correctness, #15 dry-run telemetry, #6 transitive stage inputs.

### Later - scale and richer interaction

- #37 local-model adapter and capability discovery;
- #38 secure mobile attachments, screenshots, logs and voice transcripts;
- #39 incremental indexing and project prewarming;
- #18 dynamic workflow composition instead of pruning one fixed DAG.

## Product principles

- **Evidence before context volume:** retrieve only what clears a relevance gate and keep the reason for every decision.
- **Policy before provider calls:** admission, budget and approval checks happen before dispatch and are not delegated to prompts.
- **Reproducibility before convenience:** preview and delivery are built from a committed revision, never from a mutable agent worktree.
- **Human control at durable consequences:** no automatic merge/push/deploy merely because a model reported success.
- **Small public surface:** the gateway gets typed operations and durable references, never unrestricted shell access.

## Definition of not yet MVP

Local models, rich attachments, dynamic planning, adaptive quality routing and cross-machine locking are valuable, but they do not unblock the first phone-driven loop. They stay outside the MVP until the remote control, delivery and preview gates above are closed.
