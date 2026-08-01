# Operations

## Install and preflight

Run from the engine root:

```bash
uv sync --frozen
uv run ai-platform --help
git --version
codex --version
claude --version
```

Authenticate at least one delivered CLI provider:

```bash
codex login
claude auth login
```

For sandboxed target tests on Linux, install Bubblewrap and verify `bwrap --version`. Without it, tests can run unsandboxed with a warning; do not accept that fallback for unattended remote work.

## Read-only inspection

```bash
uv run ai-platform context "Add an OAuth callback" --repo /path/to/project
uv run ai-platform route architect --complexity critical
uv run ai-platform quota
uv run ai-platform history --repo /path/to/project
```

Route inspection shows the chosen provider, requested model, effort, quota pressure, profile history, and rejected alternatives.

## Run modes

```bash
# Dogfood against this repository
uv run ai-platform run "Update model routing"

# Use another local repository
uv run ai-platform run "Add an OAuth callback" --repo /path/to/project

# Inspect the planned run without writable stages
uv run ai-platform run "Add an OAuth callback" --repo /path/to/project --dry-run
```

Dry run may call the decomposer, so it is not guaranteed to be provider-free.

## Before a run

1. Confirm the target path and current branch.
2. Decide whether uncommitted changes should remain outside the run.
3. Commit `.ai-platform.yml` with a real validation command.
4. Inspect route and quota pressure for expensive or critical work.
5. Confirm provider authentication and local disk space.
6. For unattended work, confirm the process has a supervision and recovery strategy.

The default dirty policy starts from committed HEAD, warns about the dirty checkout, and excludes uncommitted changes from run context.

## During and after a run

A mutating run creates `engine/<slug>` and temporary worktrees. The terminal report should expose stage states, validation, review, correction, delivery branch, and any retained diagnostic path.

After completion:

```bash
git log --graph --oneline --decorate engine/<slug>
git diff <base>...engine/<slug>
```

Inspect and test the delivery branch before manually merging or pushing. The platform does neither automatically.

## Telemetry and quota

History is shared at the engine root but normally filtered by target. Requested model and effective model are separate. Use the effective value when the provider reports it, while retaining the requested profile for audit.

Quota is a local estimate from recorded tokens versus declared rolling-window allowance. It is a routing signal, not a provider balance or financial ceiling.

## Durable jobs

`submit`, `status`, `jobs`, `cancel`, and `work` (issue [#24](https://github.com/4nass/ai-platform/issues/24)) are delivered and tested against `core/jobs/`. That is a statement about the local CLI/queue contract, not about remote exposure — no authentication, project allowlist, or hard budget exists yet (see [Security](security.md)), so these commands are not yet a safe surface for an untrusted remote caller.

`submit` persists the request and starts a detached worker; `status <id>` and `jobs` are readable from any process, including after the submitting terminal is closed. A job whose worker dies is reconciled to `interrupted` on the next `jobs`/`status`/`work` call, keeping its branch, base revision, and integration-worktree path. `work [--job ID]` runs one job or drains the whole queue in the foreground — the entry point a managed service unit would call once [#40](https://github.com/4nass/ai-platform/issues/40) exists.

## Troubleshooting

### A provider cannot start

Check CLI version and authentication, then run a read-only route inspection. Distinguish authentication, unsupported model/effort, timeout, quota gate, and malformed output; they require different remedies.

### The wrong model is selected

Check the run complexity, profile order in `config/agents.yaml`, quota ratio, and recent exact-profile outcomes. If all candidates are gated, the first declared profile runs deliberately.

### Target tests are skipped

Commit a valid `.ai-platform.yml` at the base revision. Skipped is not passed.

### Tests created forbidden cache files

Declare only the legitimate repository-relative patterns in `allowed_ephemeral_writes`. Do not broadly downgrade ignored writes to warnings.

### A worktree remains

Failure, conflict, interruption, or cleanup error may intentionally retain it. Confirm the reported path and branch, inspect useful changes, then use normal Git worktree cleanup. Never delete a guessed or broad temporary path.

### A run appears stuck

For the synchronous path, inspect the provider subprocess and terminal output. For the jobs path, run `ai-platform status <id>` — reconciliation runs on that read and marks an abandoned run `interrupted` once its heartbeat is stale (default 180s with no beat), rather than leaving the row saying `running` forever.

### WSL-specific problems

Use Linux-native Git, Python, and uv for a repository inside WSL. Mixing Windows processes with WSL worktrees can keep handles open and break cleanup. Reliable service startup inside WSL is tracked by [#40](https://github.com/4nass/ai-platform/issues/40).
