# AI Software Engineering Platform

Local, single-user engineering orchestration for running a specialized AI team on real Git repositories.

The platform selects relevant project context, plans a bounded workflow, routes each role to a Claude or Codex execution profile, isolates changes in Git worktrees, runs project tests, reviews the result, and records token usage and outcomes. It is designed as the engineering backend behind a future personal mobile gateway, not as a multi-tenant SaaS.

## Current status

The synchronous engineering engine is operational:

- semantic and graph-assisted context selection;
- fixed, prunable workflow DAG with specialized roles;
- Claude Code and Codex CLI adapters;
- explicit provider, model, and effort routing;
- isolated integration, stage, and validation worktrees;
- sandboxed target tests when Bubblewrap is available;
- bounded test/review correction loop;
- SQLite telemetry, token accounting, and quota pressure;
- dirty-working-tree policies with context built from the run snapshot;
- durable asynchronous jobs, detached worker, heartbeat, and crash reconciliation.

OpenClaw, project registry, authentication, approvals, hard admission budgets, preview deployments, and local-model execution remain roadmap items — the job queue above has no authentication, allowlist, or budget in front of it yet, so it is not a safe surface for an untrusted remote caller. The exact status is maintained in [Feature status](docs/feature-status.md).

## Quick start

Requirements: Python 3.11+, `uv`, Git, and at least one authenticated provider CLI.

```bash
uv sync --frozen
codex login
claude auth login

uv run ai-platform run "Add a health endpoint"
uv run ai-platform run "Add a health endpoint" --repo /path/to/project
```

Or submit the work and walk away. `run` holds a terminal for the length of a run and its state dies with the process; `submit` persists the request *before* acknowledging it, returns a job id, and starts a detached worker:

```bash
uv run ai-platform submit "Add a health endpoint" --repo /path/to/project
uv run ai-platform status 1
uv run ai-platform jobs
```

The job survives a closed terminal, a disconnect or a WSL restart, and `status` answers for it from any process. A run whose worker dies is marked `interrupted` rather than `failed`, keeping its `base_sha`, branch, stage and integration-worktree path — so work already committed stays inspectable instead of orphaned:

```bash
uv run ai-platform resume 1
```

`resume` continues that job on its own branch, skipping the stages it already merged rather than paying for them twice; `status` says which those are first. `ai-platform work` drains the queue in the foreground, which is what a managed service unit would call.

Useful read-only commands:

```bash
uv run ai-platform context "Add a health endpoint"
uv run ai-platform route architect --complexity critical
uv run ai-platform quota
uv run ai-platform history --repo /path/to/project
```

A target repository declares its validation policy in `.ai-platform.yml`:

```yaml
test_command: [uv, run, pytest, -q]
test_timeout: 120
test_sandbox: true
allowed_ephemeral_writes:
  - ".pytest_cache/**"
  - "**/__pycache__/**"
```

## Documentation

Start with the [technical documentation index](docs/README.md).

- [Product scope and terminology](docs/product-scope.md)
- [Architecture](docs/architecture.md)
- [Technology stack](docs/technology-stack.md)
- [Feature status and roadmap](docs/feature-status.md)
- [Configuration reference](docs/configuration.md)
- [Operations](docs/operations.md)
- [Testing](docs/testing.md)
- [Architecture decisions](docs/decisions/README.md)

## Safety boundary

The platform never automatically merges or pushes its delivery branch. Model output and repository content are treated as untrusted. Safety comes from provider tool restrictions, Git/worktree isolation, path contracts, frozen run policy, ignored-write detection, test sandboxing, and human review—not from prompts alone.

See [Security model](docs/security.md) and [Git and worktree isolation](docs/git-and-worktrees.md) before enabling unattended execution.

## Development

```bash
uv run pytest -q
git diff --check
```

Contributions should update the corresponding document and, when a durable architectural choice changes, add or supersede an ADR under `docs/decisions/`.
