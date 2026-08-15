# Product scope and terminology

## Purpose

AI Platform is a local-first, single-user engineering backend. It receives a software change request, selects relevant repository context, chooses a bounded agent workflow, routes each role to an available provider/model/effort profile, isolates modifications, validates the result, and leaves a delivery branch for human approval.

Its long-term role is the engineering platform behind a personal gateway reachable from a phone. The gateway handles messaging, identity, interaction, and notifications; this repository remains responsible for engineering execution, policy, artifacts and audit.

## Why this product exists

The differentiator is control over the engineering loop, not another chat interface: context is evidence-ranked and snapshot-consistent; model choice is explicit and quota-aware; every mutation is Git-isolated; budgets and approvals are deterministic; and delivery is reproducible from a committed revision. The result is a personal control plane that can be inspected and stopped, rather than an agent process that happens to edit a checkout.

## Product boundaries

### Current product

The current product is a local-first engineering engine with a CLI and an authenticated remote engine surface. It supports synchronous and durable asynchronous execution, Claude Code and Codex CLI providers, context retrieval, worktree isolation, target tests, review/correction, telemetry, project admission, HMAC principals, idempotent envelopes, structured events, REST/SSE, token/call budgets and scoped approvals. The transport server and OpenClaw adapter are implemented and tested, but production exposure is still blocked by #49.

### MVP target

The engine-side target capabilities are now present: narrow authenticated OpenClaw tools/API, structured progress events and cancellation, synchronized Git base policy, audited action plans, authenticated per-run preview lifecycle, managed service health and compact notification outbox. Remaining work is local-model policy/adapter support, complete secrets and budget ceilings, concrete gateway/provider deployment and the #49 exposure gate. The target sequence and exit criteria are maintained in [MVP trajectory](mvp-trajectory.md).

### Explicit non-goals

- multi-tenant SaaS operation;
- direct public exposure of the CLI or worker;
- unrestricted shell access from OpenClaw;
- autonomous merge or push to a protected branch;
- treating prompt instructions as a security boundary;
- guaranteeing provider subscription quota from local estimates;
- executing arbitrary repositories remotely without admission and secrets policy;
- unrestricted local-model fallback, rich attachments and dynamic workflow composition.

## Users and interfaces

| Actor | Current interface | MVP/target interface |
|---|---|---|
| Owner/developer | `ai-platform` CLI | phone, browser, messenger and CLI |
| Personal gateway | none | authenticated OpenClaw tools/API |
| Provider | Claude Code or Codex CLI | CLI, API and local adapters |
| Target project | local Git checkout or allowlisted project id | registered repository plus execution policy |
| Reviewer | terminal report and delivery branch | approval action, preview URL and notification |

## Core terminology

- **Engine root:** this repository. It contains shared prompts, routing policy, presets and cross-project telemetry.
- **Target root:** the repository supplied with `--repo` or resolved by `--project`. It owns code, target policy, local context index and generated worktrees.
- **Base revision:** the commit captured as the intended starting point of a run.
- **Integration worktree:** the run-level checkout where successful stage branches are merged.
- **Stage worktree:** a temporary checkout dedicated to one writable DAG stage.
- **Validation worktree:** a disposable checkout used to run target tests without polluting the delivery tree.
- **Delivery branch:** the retained `engine/<slug>` branch containing the run result. It is never pushed or merged automatically.
- **Role:** a specialized responsibility such as architect, backend, tests or security.
- **Profile:** an ordered `provider + model + effort` routing candidate.
- **Run:** one orchestration attempt and its stage results.
- **Job:** the durable asynchronous lifecycle around a run - `queued`/`running`/`waiting_approval`/`succeeded`/`failed`/`cancelled`/`interrupted`. A queued or cancelled job never became a run.
- **Principal:** the identity established by an authenticated transport. Today the local CLI uses the local owner boundary; the remote principal is implemented by the signed transport verifier; production gateway deployment still remains.
- **Target policy:** the base-revision `.ai-platform.yml` used for validation and ephemeral-write rules.
- **Context snapshot:** information selected from the same checkout the agents can modify.

## Success criteria

A successful local run must be reproducible from an identified base revision, keep the user's checkout untouched, contain only authorized changes, pass configured validation when one exists, complete review, record resource usage, and produce a branch a human can inspect. The remote MVP adds authenticated durable state, idempotency, hard budgets, cancellation, approval policy, synchronized delivery, immutable artifacts and a preview URL.
