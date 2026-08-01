# Testing

## Test layers

The suite separates five concerns:

1. router validation and deterministic profile selection;
2. provider command construction and response normalization;
3. scheduler propagation and telemetry;
4. decomposer parsing and bounded complexity;
5. supervisor integration with Git worktrees, DAG execution, tests, review, and correction.

Changes to `config/agents.yaml` should add or update tests at every affected boundary, not only YAML parsing.

## Standard validation

```bash
uv run --isolated --frozen pytest -q
```

The isolated environment avoids depending on a stale project virtual environment. Worktree tests must run with a Python and Git executable native to the same operating system and filesystem. In a WSL checkout, run them from WSL; Windows Python can hold directory handles and make otherwise-correct worktree cleanup fail.

Useful focused commands:

```bash
uv run --isolated --frozen pytest -q tests/test_router.py
uv run --isolated --frozen pytest -q tests/test_scheduler.py
uv run --isolated --frozen pytest -q tests/test_decomposer.py
uv run --isolated --frozen pytest -q tests/test_claude_code_adapter.py
uv run --isolated --frozen pytest -q tests/test_supervisor.py
```

Also run:

```bash
git diff --check
uv run --isolated --frozen python -m compileall -q core providers src tests
```

## What the automated tests must prove

- every configured role has valid ordered profiles;
- all complexity values are bounded and unknown values fail;
- the requested model and effort reach both provider CLIs;
- legacy keys remain readable but ambiguous duplicates fail;
- complexity reaches every worker, reviewer, and corrector call;
- requested settings and effective model metadata are recorded;
- quota and exact-profile failure fallback remain deterministic;
- read-only roles stay read-only;
- worktree isolation and correction semantics remain intact.

## Dry-route validation

Before merging a policy change, inspect all three complexity tiers for every role:

```bash
for role in decomposer architect backend frontend reviewer security tests documentation corrector; do
  for level in routine complex critical; do
    uv run ai-platform route "$role" --complexity "$level"
  done
done
```

This performs no model call.

## Real-provider smoke test

Automated adapter tests mock subprocesses; they prove command construction, not account availability. When credentials and budget permit, perform a minimal read-only call with each provider after changing model names or effort levels. Confirm:

- the CLI accepts the requested identifier;
- the reported effective model is plausible;
- the selected effort is accepted;
- telemetry contains provider, model, effort, complexity, and session data;
- no files changed for a read-only role.

Never make real-provider calls part of the default test suite. They are non-deterministic, consume quota, and depend on external authentication.
