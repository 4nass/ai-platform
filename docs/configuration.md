# Configuration reference

## Configuration layers

| File | Scope | Purpose |
|---|---|---|
| `config/agents.yaml` | engine | Ordered provider/model/effort profiles per role and complexity |
| `config/routing.yaml` | engine | Measurable quota and recent-success routing gates |
| `config/quota.yaml` | engine | Declared subscription token windows |
| `config/context.yaml` | engine | Retrieval sources, thresholds, injection mode, and budgets |
| `config/workflow.yaml` | engine | Fixed DAG, parallelism, decomposition, and correction bound |
| `prompts/<role>.md` | engine | Role instructions and structured output contract |
| `.ai-platform.yml` | target | Tests, timeout, sandbox, and allowed ephemeral writes |

Engine policy governs orchestration. Target policy governs how a particular repository is validated. The target policy is frozen from the base revision for a run.

## Agent profiles

`config/agents.yaml` maps every role to an ordered list:

```yaml
architect:
  profiles:
    - provider: codex_cli
      model: gpt-5.6-sol
      effort: high
    - provider: claude_code
      model: claude-sonnet-5
      effort: high
  profiles_by_complexity:
    routine:
      - provider: codex_cli
        model: gpt-5.6-terra
        effort: medium
    critical:
      - provider: codex_cli
        model: gpt-5.6-sol
        effort: xhigh
```

`profiles` is the `complex` policy and required fallback. Overrides accept only `routine`, `complex`, and `critical`. New policy uses `profiles` and provider-neutral `effort`; legacy `provider`, `providers`, and `reasoning_effort` remain readable for compatibility. Ambiguous duplicate fields fail validation.

Unsupported provider/effort pairs and empty profile lists are configuration errors. See [Model routing policy](model-routing-policy.md).

## Routing and quota

The shipped routing gates are:

```yaml
max_quota_ratio: 0.85
min_success_rate: 0.6
min_samples: 5
window_hours: 24
```

They may skip candidates but do not rewrite semantic profile order. If every candidate is gated, the first profile still runs with a visible forced-fallback reason.

Quota declarations are estimates:

```yaml
providers:
  claude_code:
    window_hours: 5
    tokens: 8000000
  codex_cli:
    window_hours: 5
    tokens: 8000000
```

## Context

```yaml
use_git_diff: true
use_graph: true
use_vector_db: true
use_memory: true
injection_mode: pointers
min_similarity: 0.20
min_similarity_ratio: 0.5
min_lift: 1.2
max_files: 20
max_context_chars: 20000
```

CLI providers use ranked path pointers by default because they can read the worktree. Providers without disk access receive full rendered excerpts.

## Workflow

```yaml
max_parallel: 2
decompose: true
max_correction_attempts: 1
tasks:
  - id: architecture
    agent: architect
    depends_on: []
```

Task IDs and roles must exist in the bounded workflow. The decomposer can prune tasks but cannot invent new roles.

## Target validation

Recommended target configuration:

```yaml
test_command: [uv, run, pytest, -q]
test_timeout: 120
test_sandbox: true
allowed_ephemeral_writes:
  - ".pytest_cache/**"
  - "**/__pycache__/**"
```

Use a command array. Keep allowed patterns narrow and repository-relative. Commit this file so the effective policy can be read from the run's base revision. If absent, target validation is explicitly skipped.

## Authentication and sensitive values

Subscription adapters rely on `codex login` and `claude auth login`. API adapters use provider environment credentials and separate billing. Never store secrets in YAML policy or prompts.

## Change discipline

A configuration change is a behavior change. Validate parsing, inspect every affected role/complexity route, run the test suite, and update the corresponding document. Consolidating overlapping budget and routing files is tracked by [#41](https://github.com/4nass/ai-platform/issues/41).
