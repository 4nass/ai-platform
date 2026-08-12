# AI Software Engineering Platform

AI Platform is a personal engineering backend: it turns a request into a bounded, evidence-driven change on a real Git repository. It is local-first and single-user today, and is designed to become the execution plane behind a phone-accessible OpenClaw gateway.

The platform selects relevant project context, plans a bounded workflow, routes each role to a Claude or Codex execution profile, isolates changes in Git worktrees, runs project tests, reviews the result, and records token usage and outcomes.

The product differentiates itself in five ways:

- **Context with provenance:** semantic, graph, Git-diff and memory evidence is selected with relevance gates, rendered for the provider, and taken from the same revision the agents modify.
- **Explicit model governance:** every role receives a provider, model and effort profile. Quota pressure and recent outcomes influence failover without letting an agent silently choose an ungoverned model.
- **Git-native isolation:** each run has an integration worktree and each writable stage has its own worktree. The user's checkout is not moved, and the delivery branch is the review boundary.
- **Durability with bounded spend:** asynchronous jobs survive terminal/WSL restarts, reconcile crashes, resume from merged-stage checkpoints, and reserve token/call budgets before dispatch.
- **Human-controlled delivery:** tests, review, approvals, previews and eventual Git delivery are explicit stages; prompts are never treated as a security boundary.

## Current status

The local engineering engine is operational and tested. The delivered surface includes:

- semantic and graph-assisted context selection;
- fixed, prunable workflow DAG with specialized roles;
- Claude Code and Codex CLI adapters;
- explicit provider, model, and effort routing;
- isolated integration, stage, and validation worktrees;
- sandboxed target tests when Bubblewrap is available;
- bounded test/review correction loop;
- SQLite telemetry, token accounting, and quota pressure;
- dirty-working-tree policies with context built from the run snapshot;
- durable asynchronous jobs, detached worker, heartbeat, crash reconciliation and resume;
- project registry/action allowlist, idempotent replay-safe envelopes, hard token/call admission budgets and scoped approvals for the local job path.


The authenticated REST/SSE transport and structured event stream are implemented as the remote boundary for OpenClaw. It still requires TLS, a secret manager, rate limiting and a controlled deployment boundary; remote Git synchronization, preview deployment and secrets-retention remain roadmap work. The exact split between engine-delivered capabilities and the remote roadmap is maintained in [Feature status](docs/feature-status.md).

## MVP target
The MVP is not a general-purpose SaaS or autonomous deployment system. It is a personal loop that can be used from a phone:

message -> authenticated OpenClaw tool -> durable job -> progress/approval -> tested delivery branch -> ephemeral preview URL -> human validation

The exit criteria and issue mapping are in [MVP trajectory](docs/mvp-trajectory.md). Until the MVP gates are complete, use the CLI locally and do not expose the worker to the Internet.

## Quick start

Requirements: Python 3.11+, `uv`, Git, and at least one authenticated provider CLI.

```bash
uv sync --frozen
uv run ai-platform doctor

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

Before the first run, use `ai-platform doctor` to check the local prerequisites. It prints one row per check:

- `PASS`: the prerequisite is valid;
- `WARN`: an optional capability is missing or degraded;
- `FAIL`: a reliable run is blocked, and the command exits with status 1.

Use `--repo /path/to/project` (or `--project <id>`) to include a target repository in the preflight.

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
- [MVP objectives and trajectory](docs/mvp-trajectory.md)
- [Architecture](docs/architecture.md)
- [REST/SSE API contract](docs/api-contract.md)
- [REST/SSE implementation and deployment](docs/api-rest-sse.md)
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
