# Testing

## Test strategy

The suite covers policy and data contracts, provider adapters, context selection, orchestration, and real Git isolation. It uses deterministic fake providers for default tests; live model calls are deliberately excluded.

| Layer | What it proves |
|---|---|
| Configuration | Valid roles, profiles, efforts, DAG, and target policy |
| Routing | Ordered selection, gates, forced fallback, telemetry metadata |
| Provider adapters | Command construction, read-only flags, normalized results |
| Context | Chunking, retrieval thresholds, graph/cache safety |
| Scheduler | Dependencies, concurrency, skip and failure propagation |
| Git integration | Checkout isolation, worktrees, contracts, merges, cleanup |
| Validation | Frozen policy, sandbox command, ignored-write handling |
| Supervisor | End-to-end run, review, correction, final report |
| Jobs | State transitions, idempotency, atomic claim, heartbeat thread, reconciliation, crash-induced hook-lock repair, cancellation |

## Standard verification

```bash
uv run --isolated --frozen pytest -q
git diff --check
uv run --isolated --frozen python -m compileall -q core providers src tests
```

Run from WSL for a WSL checkout. Windows Python or Git can keep directory handles open and cause false worktree-cleanup failures.

Useful focused runs:

```bash
uv run --isolated --frozen pytest -q tests/test_router.py
uv run --isolated --frozen pytest -q tests/test_decomposer.py
uv run --isolated --frozen pytest -q tests/test_supervisor.py
uv run --isolated --frozen pytest -q tests/test_git_ops.py
uv run --isolated --frozen pytest -q tests/test_target_config.py
```

Test filenames evolve; use `rg --files tests` to discover the current set.

## Required invariants

Automated tests should prove:

- the original checkout keeps its branch, HEAD, tracked changes, and untracked changes;
- run context comes from the same integration snapshot that agents modify;
- frozen target policy cannot be weakened by same-run changes;
- every writable stage uses an isolated worktree;
- tracked, untracked, and ignored writes are inventoried;
- allowed ephemeral caches are disposable and unknown ignored writes fail;
- failed stages cannot leak changes and dependents are skipped;
- merge conflicts and interruptions retain diagnosable artifacts;
- model, effort, complexity, tokens, outcome, and routing reason reach telemetry;
- read-only roles cannot modify files;
- target tests run from the integrated revision in a disposable checkout;
- correction is bounded and only used for eligible failures;
- cleanup and finalization are idempotent.

## Routing policy checks

After changing a profile preset (`config/presets/profiles/<name>.yaml`), inspect all role/class combinations:

```bash
for role in decomposer architect backend frontend reviewer security tests documentation corrector; do
  for level in routine complex critical; do
    uv run ai-platform route "$role" --complexity "$level"
  done
done
```

This does not launch implementation agents. It may read telemetry to explain current gate decisions.

## Real-provider smoke tests

Mocks prove adapter contracts, not account availability or current model names. After changing provider flags, model identifiers, or effort semantics, make one minimal read-only call per provider when credentials and quota permit. Verify accepted options, effective model, session metadata, and zero filesystem changes.

Live calls must never be part of the default suite: they are non-deterministic, externally billed or quota-limited, and require personal credentials.

## Documentation verification

Documentation changes must pass:

- `git diff --check`;
- local Markdown link validation;
- Mermaid syntax inspection for changed diagrams;
- feature-status review so planned behavior is not described as delivered.

Do not hardcode the passing test count in documentation; the suite changes frequently.
