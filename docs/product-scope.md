# Product scope and terminology

## Purpose

AI Platform is a local, single-user engineering backend. It receives a software change request, selects relevant repository context, chooses a bounded agent workflow, routes each role to an available model profile, isolates modifications, validates the result, and leaves a delivery branch for human approval.

Its long-term role is the engineering platform behind a personal gateway reachable from a phone. The gateway handles messaging, identity, interaction, and notifications; this repository remains responsible for engineering execution.

## Product boundaries

### Current product

The current product is a command-line engine that runs against a local Git repository. It supports synchronous *and* durable asynchronous execution (`run` vs. `submit`/`status`/`jobs`/`cancel`/`work`, `core/jobs/`), Claude Code and Codex CLI providers, context retrieval, worktree isolation, target tests, review/correction, and telemetry. The job queue survives a disconnect or restart and reconciles an abandoned run to `interrupted`; it has no authentication, project allowlist, or hard budget in front of it yet, so it is not a safe surface for an untrusted remote caller.

### Target product

The target architecture adds an authenticated, idempotent gateway API in front of the existing job queue, a project registry, approvals, hard budgets, preview environments, notifications, and an OpenClaw integration. Those capabilities are not considered delivered until their issue is closed and their status changes in [Feature status](feature-status.md).

### Explicit non-goals today

- multi-tenant SaaS operation;
- direct public exposure of the CLI;
- autonomous merge or push to a protected branch;
- treating prompt instructions as a security boundary;
- guaranteeing provider subscription quota from local estimates;
- executing arbitrary repositories remotely without an admission and secrets policy.

## Users and interfaces

| Actor | Current interface | Target interface |
|---|---|---|
| Owner/developer | `ai-platform` CLI | phone, browser, and CLI |
| Personal gateway | none | authenticated OpenClaw tools/API |
| Provider | Claude Code or Codex CLI | CLI, API, and local adapters |
| Target project | local Git checkout | registered repository plus execution policy |
| Reviewer | terminal report and delivery branch | approval UI, preview URL, and notifications |

## Core terminology

- **Engine root**: this repository. It contains shared prompts, routing policy, and cross-project telemetry.
- **Target root**: the repository supplied with `--repo`. It owns code, target policy, local context index, and generated worktrees.
- **Base revision**: the commit captured as the intended starting point of a run.
- **Integration worktree**: the run-level checkout where successful stage branches are merged.
- **Stage worktree**: a temporary checkout dedicated to one writable DAG stage.
- **Validation worktree**: a disposable checkout used to run target tests without polluting the delivery tree.
- **Delivery branch**: the retained `engine/<slug>` branch containing the run result. It is never pushed or merged automatically.
- **Role**: a specialized responsibility such as architect, backend, tests, or security.
- **Profile**: an ordered `provider + model + effort` routing candidate.
- **Run**: one orchestration attempt and its stage results.
- **Job**: the durable asynchronous lifecycle around a run — `queued`/`running`/`waiting_approval`/`succeeded`/`failed`/`cancelled`/`interrupted` (`core/jobs/`, delivered). Distinct from a `Run`: a queued or cancelled job never became one.
- **Target policy**: the base-revision `.ai-platform.yml` used for validation and ephemeral-write rules.
- **Context snapshot**: information selected from the same checkout the agents can modify.

## Success criteria

A successful run must be reproducible from an identified base revision, keep the user's checkout untouched, contain only authorized changes, pass configured validation when one exists, complete review, record resource usage, and produce a branch a human can inspect. Remote operation adds durable state, authentication, idempotency, hard budgets, cancellation, approval gates, and auditable artifact delivery.
