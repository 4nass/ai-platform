# Operations

## Install and preflight

Run from the engine root:

```bash
uv sync --frozen
uv run ai-platform doctor
git --version
codex --version
claude --version
```

`doctor` distinguishes `PASS` (valid prerequisite), `WARN` (optional or degraded capability), and `FAIL` (a reliable run is blocked). It exits non-zero if any check is `FAIL`. To include a target repository, pass `--repo <path>` or `--project <id>`; the default target is the current directory.

When `uv` is installed but missing from `PATH`, doctor prints the exact `export PATH=...` command for the current shell and the Bash/WSL commands to persist it in `~/.bashrc`.

Authenticate at least one delivered CLI provider:

```bash
codex login
claude auth login
```

For sandboxed target tests on Linux, install Bubblewrap and verify `bwrap --version`. Without it, tests can run unsandboxed with a warning; do not accept that fallback for unattended remote work.

## Admission and inspection

Use `--repo` for a local owner naming a path. Use `--project` for the allowlisted form intended for queued or future remote requests:

```bash
uv run ai-platform context "Add an OAuth callback" --repo /path/to/project
uv run ai-platform context "Add an OAuth callback" --project dogfooding
uv run ai-platform route architect --complexity critical
uv run ai-platform quota
uv run ai-platform history --project dogfooding
```

Project resolution happens before indexing or provider selection and is re-checked when a queued job is claimed. Route inspection shows the chosen provider, requested model, effort, quota pressure, profile history and rejected alternatives.

## Run modes

```bash
# Dogfood against this repository
uv run ai-platform run "Update model routing"

# Use another local repository
uv run ai-platform run "Add an OAuth callback" --repo /path/to/project

# Use a registered project
uv run ai-platform submit "Add an OAuth callback" --project dogfooding

# Inspect the planned run without writable stages
uv run ai-platform run "Add an OAuth callback" --repo /path/to/project --dry-run
```

Dry run may call the decomposer, so it is not guaranteed to be provider-free.

## Before a run

1. Confirm the target path or project id and current branch.
2. Decide whether uncommitted changes should remain outside the run.
3. Commit `.ai-platform.yml` with a real validation command.
4. Inspect route and quota pressure for expensive or critical work.
5. Confirm provider authentication and local disk space.
6. For unattended work, confirm supervision, recovery and Bubblewrap prerequisites.

The default dirty policy starts from committed HEAD, warns about the dirty checkout, and excludes uncommitted changes from run context.

## During and after a run

A mutating run creates `engine/<slug>` and temporary worktrees. The terminal report should expose stage states, validation, review, correction, delivery branch and any retained diagnostic path.

After completion:

```bash
git log --graph --oneline --decorate engine/<slug>
git diff <base>...engine/<slug>
```

Inspect and test the delivery branch before manually merging or pushing. The platform does neither automatically.

## Telemetry, quota and budgets

History is shared at the engine root but normally filtered by target. Requested model and effective model are separate. Use the effective value when the provider reports it, while retaining the requested profile for audit.

Quota is a local estimate from recorded tokens versus declared rolling-window allowance. It is a routing signal, not a provider balance or financial ceiling. Hard token/call budgets are a separate admission gate: reservations happen before provider dispatch, `strict` pauses for approval, and `local_fallback` waits because no local adapter exists yet. Time and currency ceilings are not implemented.

## Durable jobs

`submit`, `status`, `jobs`, `cancel`, `work`, `resume`, `approvals`, `approve` and `deny` are delivered and tested against `core/jobs/`. This is the local CLI/queue contract, not remote exposure. Project allowlisting, idempotent submission, hard token/call budgets and scoped approvals exist in the engine; an authenticated non-local transport does not yet exist. See [Security](security.md), [MVP trajectory](mvp-trajectory.md), #29 and #30.

`submit` persists the request and starts a detached worker; `status <id>` and `jobs` are readable from any process, including after the submitting terminal is closed. A job whose worker dies is reconciled to `interrupted`, keeping its branch, base revision and integration-worktree path. `work [--job ID]` runs one job or drains the queue in the foreground, which is the entry point a managed service unit will call once #40 is delivered.

When a run stops for a decision rather than a fault, such as a `strict` budget overrun, it lands in `waiting_approval` with a request attached:

```bash
uv run ai-platform approvals
uv run ai-platform approve 3
uv run ai-platform resume 7
```

An approval is bound to the exact inputs shown, is single-use and expires. If the diff, target, command or amount changes, a new decision is required.

`resume <id>` continues an interrupted job rather than starting it over. Stages already merged onto its branch are not run again; verification and review are re-run against the resumed tree. A failed job ran to a verdict and requires a new request describing the fix.

A stage that was mid-flight when the worker died leaves an `engine-task/...` worktree behind. Resume names it rather than deleting it because it may hold uncommitted agent work; inspect it and remove it with normal Git worktree commands when done.

## Troubleshooting

### A provider cannot start

Check CLI version and authentication, then run a read-only route inspection. Distinguish authentication, unsupported model/effort, timeout, quota gate and malformed output; they require different remedies.

### The wrong model is selected

Check run complexity, profile order in `config/presets/profiles/<name>.yaml`, quota ratio and recent exact-profile outcomes. If all candidates are gated, the first declared profile runs deliberately.

### Target tests are skipped

Commit a valid `.ai-platform.yml` at the base revision. Skipped is not passed.

### Tests created forbidden cache files

Declare only legitimate repository-relative patterns in `allowed_ephemeral_writes`. Do not broadly downgrade ignored writes to warnings.

### A worktree remains

Failure, conflict, interruption or cleanup error may intentionally retain it. Confirm the reported path and branch, inspect useful changes, then use normal Git worktree cleanup. Never delete a guessed or broad temporary path.

### A run appears stuck

For the synchronous path, inspect the provider subprocess and terminal output. For jobs, run `ai-platform status <id>`; reconciliation marks an abandoned run `interrupted` once its heartbeat is stale (default 180 seconds). From there, `ai-platform resume <id>` continues it.

### WSL-specific problems

Use Linux-native Git, Python and uv for a repository inside WSL. Mixing Windows processes with WSL worktrees can keep handles open and break cleanup. Reliable service startup inside WSL is tracked by #40.

## Remote warning

OpenClaw/API, structured events, remote Git delivery, preview deployment and secrets retention are not delivered. Do not expose the local worker to the Internet; follow the gates in [Security](security.md) and [MVP trajectory](mvp-trajectory.md).
