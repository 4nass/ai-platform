# Technical documentation

This directory is the source of truth for the platform's technical design, operational behavior, feature status, and architectural decisions. The root README intentionally contains only the product summary, quick start, and links into this documentation.

## Read in this order

1. [Product scope and terminology](product-scope.md) — what the product is and is not.
2. [Feature status](feature-status.md) — implemented, in progress, and planned capabilities.
3. [Architecture](architecture.md) — component boundaries and end-to-end flows.
4. [Security model](security.md) — trust boundaries and enforcement layers.
5. [Operations](operations.md) — how to run and troubleshoot it.

## Component documentation

| Area | Document | Code/config source of truth |
|---|---|---|
| Runtime and dependencies | [Technology stack](technology-stack.md) | `pyproject.toml`, `uv.lock` |
| Context selection and project graph | [Context engineering](context-engineering.md) | `core/context/`, `core/graph/`, `config/context.yaml` |
| Planning, agents, DAG, correction | [Workflow and agents](workflow-and-agents.md) | `core/orchestrator/`, `config/workflow.yaml`, `prompts/` |
| Branches, worktrees, dirty trees | [Git and worktree isolation](git-and-worktrees.md) | `core/orchestrator/git_ops.py`, `supervisor.py` |
| Providers, models, effort | [Providers and routing](providers-and-routing.md) | `providers/`, `config/agents.yaml`, `config/routing.yaml` |
| Detailed routing policy | [Model and effort routing policy](model-routing-policy.md) | `config/agents.yaml` |
| Tests and target policy | [Validation and sandboxing](validation.md) | `.ai-platform.yml`, `target_config.py`, `test_runner.py` |
| Telemetry, jobs, tokens, quotas | [Data, telemetry, and budgets](data-and-observability.md) | `core/telemetry/`, `core/jobs/`, SQLite files |
| Configuration | [Configuration reference](configuration.md) | `config/*.yaml`, `.ai-platform.yml` |
| Verification | [Testing](testing.md) | `tests/`, `pyproject.toml` |

## Governance and history

- [Architecture decision records](decisions/README.md) explain why durable choices were made.
- [Feature status](feature-status.md) separates delivered behavior from target architecture.
- [Known limitations](known-limitations.md) records intentional boundaries and unresolved risks.
- `CHANGELOG.md` records shipped changes chronologically.
- `memory/adr/` contains older agent-memory ADRs; new architectural decisions belong under `docs/decisions/`.

## Documentation contract

When code and documentation disagree, code and tests describe current behavior. Correct the documentation in the same change. A document must not describe a roadmap feature as implemented; use the status labels defined in [Feature status](feature-status.md).
