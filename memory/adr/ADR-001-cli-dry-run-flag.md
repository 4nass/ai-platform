# ADR-001: `--dry-run` flag for `ai-platform run`

## Status

Accepted — implemented in `core/orchestrator/supervisor.py`'s `run()` (see Roadmap in README.md).

## Context

`ai-platform run "<request>"` (`src/ai_platform/__init__.py`) currently has one
path: `core/orchestrator/supervisor.run()` always goes all the way through —
plan, decompose, create an `engine/<slug>` branch, execute every selected
task's agent in its own worktree, commit, run tests, run review — before
returning a report. There is no way to see what a request *would* do without
those side effects.

Concretely, `supervisor.run()` (core/orchestrator/supervisor.py:104) does, in
order:

1. `planner.plan(repo_root)` — loads `config/workflow.yaml` into a `Plan`
   (the full task DAG: `architecture`, `backend`, `frontend`, `tests`,
   `security`, `documentation`, per `config/workflow.yaml`).
2. If `Plan.decompose` is true, runs the `decomposer` role (via
   `scheduler.run_task`, a real provider call) to get a `TASKS:` line
   (`core/orchestrator/decomposer.py`), then `planner.prune()`s the plan down
   to that subset.
3. Creates a git branch (`git_ops.create_branch`) and executes every
   remaining task's agent (`scheduler.run_task`) in its own worktree —
   the step that actually invokes providers and modifies the repo.
4. Runs tests and the `reviewer` role, and returns a `RunReport`.

Steps 1 and 2 are pure planning: they read `config/workflow.yaml` and make
one LLM call to decide which tasks apply. Step 3 is where agents that write
code actually run.

## Decision

Add a `--dry-run` flag to the `run` command (`src/ai_platform/__init__.py`)
that executes steps 1 and 2 only — producing and printing the pruned `Plan`
(task ids, agents, `depends_on`) and the decomposer's `TASKS:` selection
(including which known task ids were dropped, mirroring the
`console.print(f"[bold]Decomposed to:[/bold] ...")` line already in
`supervisor.run()`) — then exits without reaching step 3: no branch is
created, no worktrees are created, and no *workflow task* agent
(`architect`, `backend`, `frontend`, `tests`, `security`, `documentation`) is
invoked, so the repo is left untouched.

This means `--dry-run` does not skip *every* agent call: producing the
decomposer's selection requires the one `decomposer` role call that already
happens in step 2 today. "Without actually invoking any agent" refers to the
workflow's task agents (the ones in `config/workflow.yaml`, capable of
touching the repo) — not the decomposer, which only ever returns a
`TASKS:` line and never has file-write access.

If `Plan.decompose` is false (`config/workflow.yaml: decompose: false`),
`--dry-run` prints the full, unpruned plan with no decomposer call at all.

## Consequences

- Requires a `supervisor` entry point that stops after decomposition (either
  a `dry_run: bool` parameter on `supervisor.run()`, or a separate function)
  and a plan/selection to print — this is implementation work for the
  `backend` role, not covered by this ADR.
- `--dry-run` still performs repo indexing/context selection
  (`ContextManager`) and one decomposer LLM call, so it is not free of cost
  or of network calls — only free of repo mutation.
- Because no branch or worktree is created, `--dry-run` does not need
  `git_ops.ensure_clean_worktree` / `prune_worktrees` to run first.
