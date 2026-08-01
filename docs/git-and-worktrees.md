# Git and worktree isolation

## Goal

A run must not switch or overwrite the user's checked-out branch. Git worktrees isolate concurrent stages and make the committed delivery branch the inspectable artifact.

## Worktree hierarchy

```text
target checkout (left on its current branch)
└── integration worktree: engine/<run-slug>
    ├── stage worktree: engine-task/<run>/<stage>
    ├── stage worktree: engine-task/<run>/<stage>
    └── validation worktree: detached disposable checkout
```

The integration branch starts from the selected base. Each writable stage starts from the integration state available after its dependencies. Successful stage changes are committed and merged with a non-fast-forward merge. This retains provenance by stage.

## Lifecycle

1. Resolve the target and capture run identity.
2. Acquire the repository mutation lock.
3. Evaluate the dirty-tree policy.
4. Create `engine/<slug>` and its integration worktree.
5. Create and execute isolated stage worktrees.
6. Verify path contracts, commit, and merge successful stages.
7. Validate in a disposable worktree.
8. On success, remove temporary worktree directories but retain the delivery branch.
9. On failure or conflict, retain the relevant paths and show them in the report.

The platform never automatically pushes the delivery branch or merges it into the user's branch.

## Dirty checkout policy

The default `head` policy tolerates a dirty user checkout with a warning. The run starts from committed HEAD and context is built from the integration snapshot, so uncommitted user changes are neither modified nor shown to the agents.

A `reject` policy may require a clean tree. A coherent `snapshot` mode is declared conceptually but is not fully implemented; it must capture code and context together without silently committing user work.

## Contracts and ignored files

Tracked and untracked changes are evaluated against each role's allowed paths. Gitignored writes are also inspected because caches and generated files can hide unintended side effects.

Legitimate tool caches must be explicitly allowed by target policy, for example:

```yaml
allowed_ephemeral_writes:
  - ".pytest_cache/**"
  - "**/__pycache__/**"
```

Unknown ignored writes fail validation. Allowed ephemeral writes do not become part of the delivery branch.

## Locking and concurrency

A local `flock` serializes mutating runs for the same repository on one machine. This prevents two local supervisors from racing over branches, worktrees, or hooks. It does not coordinate two machines sharing a network filesystem.

Parallelism inside one run is still supported through independent stage worktrees and the configured DAG limit.

## Hooks

The platform controls hooks to prevent target repository hooks from executing unexpectedly during automated commits and merges. The current implementation can modify repository-wide `core.hooksPath` during a run, which may affect a simultaneous manual user commit. A per-command hook policy would provide a stronger isolation boundary.

## Known edge cases

- Worktree creation must use the base commit captured for the run, not a later moving `HEAD`.
- Target-local context indexing can still write `.ai-platform/` in the original checkout.
- Top-level crashes must finalize state and report retained worktrees consistently.
- Cleanup should be idempotent after process interruption.
- Cross-machine locking is not implemented.

See [Known limitations](known-limitations.md) and the decision [ADR-002](decisions/ADR-002-git-worktree-isolation.md).
