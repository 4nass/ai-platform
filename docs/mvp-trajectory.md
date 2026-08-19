# MVP objectives and trajectory

## Objective

Make the personal engineering loop usable away from the workstation without turning the project into a public SaaS:

```text
phone / messenger
        -> OpenClaw
        -> authenticated REST/SSE operation
        -> durable job and replayable events
        -> isolated run, tests and review
        -> explicit provider/model policy
        -> approval when required
        -> pushed delivery revision
        -> ephemeral authenticated preview
        -> phone notification and human validation
```

The platform remains the authority for project admission, context, provider/model/effort routing, local-model eligibility, token budgets, worktrees, validation, artifacts and audit. OpenClaw is only the interaction gateway.

## What is already usable

The local CLI can run synchronous work or submit a durable job. It can select context, route Claude Code/Codex CLI profiles, isolate each run in Git worktrees, validate in a disposable checkout, review/correct within a bound, record telemetry, enforce token/call budgets, gate scoped approvals, recover a crashed worker and resume merged stages. These capabilities are covered by the automated suite and are safe inside the documented single-user workstation boundary.

## MVP exit criteria

| Gate | Exit criterion | Tracking |
|---|---|---|
| Admission | A remote caller authenticates as a principal; only an allowlisted project id and authorized operation are accepted; retries are idempotent | Engine half delivered in #25/#26; transport auth #44 |
| Gateway | A small authenticated REST API accepts commands and SSE replays lifecycle events without exposing shell or paths | #29, #30, #47 |
| Execution | The run starts from a synchronized, pinned base revision and produces a remote delivery branch without mutating the user's checkout | #33 |
| Provider policy | Claude/Codex remain available and a local provider can handle explicitly allowed low-risk tasks with measured quality; no silent provider fallback | #37, #48 |
| Safety | Token/call/time/currency ceilings, secret redaction/retention, fail-closed sandbox policy and auditable approvals are enforced | #27, #28, #35, #45, #46, #49 |
| Validation | A successful delivery revision is deployed by CI/CD to an authenticated, ephemeral preview URL with expiry and teardown | #34 |
| Operability | The worker starts as a managed local user service, survives restart, exposes health/backup status and emits a compact result view/notification | #40, #42 |

The MVP is complete only when every gate has an end-to-end test through the remote contract. A local implementation of a gate is not the same thing as a reachable mobile product surface.

## Ordered roadmap

### Now - remote control and safety loop (P0/P1)

1. **#44 - authenticated transport and verified principals:** establish identity outside prompt text and protect channel identifiers.
2. **#45 - time and currency ceilings:** complete the hard admission dimensions beyond delivered token/call reservations.
3. **#35/#49 - secrets and remote-readiness gate:** redact, retain and review the complete exposure boundary before enabling a gateway.
4. **#47 - REST/SSE API:** expose submit, status, replayable events, cancellation, approvals and artifacts through a narrow contract.
5. **#29/#30 - lifecycle and OpenClaw:** make stage, provider, budget, validation, review and approval transitions observable and consumable by OpenClaw.
6. **#37/#48 - constrained local models:** add Ollama first, with an explicit low-risk role policy and acceptance evaluation; vLLM/llama.cpp use the same compatible adapter where practical.
7. **#33/#46 - Git delivery and approved actions:** pin the base ref, define divergence policy and route push/PR actions through the shared executor.
8. **#34 - preview environments:** build the committed delivery revision, deploy a short-lived authenticated subdomain and tear it down deterministically.
9. **#40/#42 - service and notifications:** keep one worker healthy on Linux, WSL2 or macOS and return compact mobile-friendly result views.

### Next - reliability and quality (P1/P2)

- #31 live provider failover, retry classification and circuit breakers;
- #32 quality-aware routing based on accepted outcomes, not process success alone;
- #36 deterministic multi-turn references for projects, runs, diffs and approvals;
- #21 CI, automated PR checks and exercised security analysis;
- #22 package `core/` and `providers/` into the built wheel;
- #14 review usefulness/factual correctness, #15 dry-run telemetry, #6 transitive stage inputs.

### Later - scale and richer interaction

- #38 secure mobile attachments, screenshots, logs and voice transcripts;
- #39 incremental context indexing and project prewarming;
- #18 dynamic workflow composition instead of pruning one fixed DAG;
- adaptive quality routing and cross-machine locking.

## Product principles

- **Evidence before context volume:** retrieve only what clears a relevance gate and keep the reason for every decision.
- **Policy before provider calls:** admission, budget, provider eligibility and approval checks happen before dispatch and are not delegated to prompts.
- **Local does not mean unrestricted:** local models are an explicit capability tier, not a silent bypass of safety or quality policy.
- **Reproducibility before convenience:** preview and delivery are built from a committed revision, never from a mutable agent worktree.
- **Human control at durable consequences:** no automatic merge/push/deploy merely because a model reported success.
- **Small public surface:** the gateway gets typed operations and durable references, never unrestricted shell access.

## Definition of not yet MVP

Rich attachments, dynamic planning, adaptive quality routing and cross-machine locking remain outside the first expanded MVP. Local models are no longer outside scope, but their use is limited to the explicit roles and evaluation gate defined by #48.
