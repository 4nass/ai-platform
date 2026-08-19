# Technical documentation

This directory is the source of truth for the platform's technical design, operational behavior, feature status, and architectural decisions. The root README intentionally contains only the product summary, quick start, and links into this documentation.

## Product north star

The product is a personal software-engineering control plane, not a chat bot and not a multi-tenant SaaS. A request arriving from a phone should be able to create a durable run, select the right context and model effort, keep spend bounded, ask for approval at consequential boundaries, and return a reproducible branch plus a browser-validatable preview. OpenClaw owns interaction and channel delivery; AI Platform owns engineering truth, policy, execution and audit.

The current implementation is the local execution half of that loop. The remote control loop is deliberately documented as a sequence of gates rather than implied by the presence of a local job queue.

## Read in this order

1. [Product scope and terminology](product-scope.md) - what the product is and is not.
2. [Feature status](feature-status.md) - implemented, in progress, and planned capabilities.
3. [MVP objectives and trajectory](mvp-trajectory.md) - exit criteria and ordered next features.
4. [Architecture](architecture.md) - component boundaries and end-to-end flows.
5. [REST/SSE API contract](api-contract.md) - stable interface for OpenClaw, browser and CLI adapters.
6. [Audited external actions](action-executor.md) - approval-gated delivery and integration boundary.
7. [Per-run previews](preview-environments.md) - immutable commits, authenticated URLs, TTL and cleanup.
8. [Security model](security.md) - trust boundaries and enforcement layers.
9. [Operations](operations.md) - how to run and troubleshoot it.

## Component documentation

| Area | Document | Code/config source of truth |
|---|---|---|
| Runtime and dependencies | [Technology stack](technology-stack.md) | `pyproject.toml`, `uv.lock` |
| Context selection and project graph | [Context engineering](context-engineering.md) | `core/context/`, `core/graph/`, `config/presets/context/` |
| Planning, agents, DAG, correction | [Workflow and agents](workflow-and-agents.md) | `core/orchestrator/`, `config/presets/workflow/`, `prompts/` |
| Branches, worktrees, dirty trees | [Git and worktree isolation](git-and-worktrees.md) | `core/orchestrator/git_ops.py`, `supervisor.py` |
| Providers, models, effort | [Providers and routing](providers-and-routing.md) | `providers/`, `config/presets/profiles/`, `config/platform.yaml` |
| Detailed routing policy | [Model and effort routing policy](model-routing-policy.md) | `config/presets/profiles/` |
| Tests and target policy | [Validation and sandboxing](validation.md) | `.ai-platform.yml`, `target_config.py`, `test_runner.py` |
| Telemetry, jobs, tokens, quotas | [Data, telemetry, and budgets](data-and-observability.md) | `core/telemetry/`, `core/jobs/`, SQLite files |
| Configuration | [Configuration reference](configuration.md) | `config/*.yaml`, `.ai-platform.yml` |
| Remote interface | [REST/SSE API contract](api-contract.md) and [implementation](api-rest-sse.md) | core/transport/ |
| External actions | [Audited action executor](action-executor.md) | core/actions/executor.py, core/jobs/approvals.py |
| Previews | [Per-run preview environments](preview-environments.md) | core/previews/manager.py, core/transport/http.py |
| Verification | [Testing](testing.md) | `tests/`, `pyproject.toml` |

## Governance and history

- [Architecture decision records](decisions/README.md) explain why durable choices were made.
- [Feature status](feature-status.md) separates delivered behavior from target architecture.
- [MVP trajectory](mvp-trajectory.md) is the product plan; GitHub issues remain the execution tracker.
- [Known limitations](known-limitations.md) records intentional boundaries and unresolved risks.
- `CHANGELOG.md` records shipped changes chronologically.
- `memory/adr/` contains older agent-memory ADRs; new architectural decisions belong under `docs/decisions/`.

## Documentation contract

When code and documentation disagree, code and tests describe current behavior. Correct the documentation in the same change. A document must not describe a roadmap feature as implemented; use the status labels defined in [Feature status](feature-status.md).
