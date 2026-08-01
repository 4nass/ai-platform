# Architecture

## Scope

AI Platform is a local, single-user engineering orchestrator. It coordinates specialized agents that inspect and modify a Git repository, validates their changes, and records the result. It has no application server and no multi-tenant control plane.

Two roots must remain distinct:

- **engine root**: this repository, containing prompts, policies, and shared telemetry;
- **target root**: the repository passed with `--repo`, containing the code, project index, worktrees, and test configuration.

They are the same only when the platform dogfoods itself.

## Execution flow

```text
request
  -> context selection
  -> decomposer: task subset + run complexity
  -> fixed workflow DAG
  -> router: role + complexity -> ordered profiles -> healthy candidate
  -> scheduler: prompt + context + model + effort
  -> isolated stage worktree
  -> merge completed stage branch
  -> target tests
  -> read-only review
  -> bounded correction loop when eligible
  -> telemetry and final report
```

The decomposer does not create an arbitrary plan. It selects tasks from the workflow declared in `config/workflow.yaml`, bridges dependencies across omitted tasks, and emits one of `routine`, `complex`, or `critical`. Missing or malformed complexity output falls back to `complex`.

## Isolation and write safety

Each writable DAG stage runs on its own temporary Git worktree and branch. Successful changes are committed and merged back with a non-fast-forward merge. A failed stage cannot leak its partial edits into the integration branch. Conflicting branches remain available for manual inspection.

Role contracts restrict which paths a stage may change. The supervisor compares the resulting diff with the role contract and rejects violations. Reviewer and security roles are additionally launched with provider-level read-only controls.

The corrector runs only after all DAG stages completed and only when target tests or the final review fail. Its attempt count is bounded by `max_correction_attempts`.

## Context layer

The context manager indexes the target repository under `.ai-platform/`, using syntax-aware chunking where supported, semantic retrieval, dependency-graph signals, the current Git diff, and target-local project memory. Selection is advisory: when no candidate clears the relevance floor, the agent receives no injected file selection and explores the worktree itself.

Generated target data:

```text
<target>/.ai-platform/
  vector/
  graph.json
```

Shared execution history stays at the engine root because provider quotas span all target repositories.

## Provider boundary

The orchestrator sends an `AgentTask` to a provider adapter. The task carries the role, description, context, worktree, requested model, requested effort, and complexity. The provider must return a normalized result and leave authorized file changes on disk.

Active subscription-backed adapters are:

- Claude Code through `claude -p`;
- Codex CLI through `codex exec --json`.

The Anthropic API adapter exists but requires separately billed API credentials. The OpenAI API adapter remains a stub.

## Failure semantics

- A provider/profile may be skipped for quota pressure or recent failures.
- If all profiles are gated, routing still selects the first declared profile and marks the fallback reason.
- A failed DAG stage skips its dependents.
- A merge conflict preserves the worktree and makes the run need attention.
- Test or review failure may enter correction; a DAG failure may not.
- Dry runs inspect the plan and routing without launching writable agents.
