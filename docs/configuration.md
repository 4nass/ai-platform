# Configuration reference

## Configuration layers

| File | Scope | Purpose |
|---|---|---|
| `config/platform.yaml` | engine | The five knobs a user actually tunes: profile, quotas, routing gates, workflow mode/parallelism/correction bound, context mode |
| `config/projects.yaml` | engine | Which repositories may be reached by id, and what may be done to each — the admission allowlist |
| `config/presets/profiles/<name>.yaml` | engine | Ordered provider/model/effort profiles per role and complexity — calibrated policy, versioned with the engine |
| `config/presets/workflow/<name>.yaml` | engine | DAG shape (task ids, roles, dependencies) |
| `config/presets/context/<name>.yaml` | engine | Retrieval sources, relevance floors, injection mode, budget |
| `prompts/<role>.md` | engine | Role instructions and structured output contract |
| `.ai-platform.yml` | target | Tests, timeout, sandbox, and allowed ephemeral writes |

Engine policy governs orchestration. Target policy governs how a particular repository is validated. The target policy is frozen from the base revision for a run; `platform.yaml` and the presets it selects are loaded once per run and threaded through, so the whole run is judged against one consistent snapshot (`core/orchestrator/platform_config.py`, [ADR-008](decisions/ADR-008-platform-config-and-presets.md)).

Run `ai-platform config` to see the resolved policy — which preset is active and its numbers — without spending a token.

## `config/projects.yaml`

Separate from `platform.yaml` on purpose ([ADR-010](decisions/ADR-010-project-registry-as-the-admission-boundary.md)): that file is tuning, where a mistake changes how well runs go; this one is an allowlist, where a mistake changes what can be reached at all.

```yaml
roots:
  - ~/workspace          # every project path must resolve to somewhere under one of these

projects:
  ai-platform:
    path: ~/workspace/ai-platform
    remote: https://github.com/4nass/ai-platform.git   # optional; verified when set
    base_branch: main                                   # optional; verified when set
    sync_policy: offline                                # offline | fetch | require_up_to_date
    allowed_actions: [inspect, modify, test]
    budget_class: standard
```

| Action | Grants |
|---|---|
| `inspect` | Read-only: context selection, routing explanation, history. The default when nothing is declared. |
| `modify` | Run the DAG — branches, worktrees, agent writes, commits. |
| `test` | Execute the target's own declared test command. A separate grant because it is arbitrary code execution on this machine, not a consequence of being writable. |
| `open_pr` | Push and open a pull request. Declarable but not implemented ([#33](https://github.com/4nass/ai-platform/issues/33)) — so a project can withhold it before it exists. |

```bash
uv run ai-platform run "Add a health endpoint" --project ai-platform
```

`--project <id>` is the only form anything arriving over a wire may use: the caller names an id and the engine decides what it refers to. `--repo <path>` remains for local interactive use, where the person running the command could already `cd` there. Passing both is refused.

Paths are resolved (symlinks followed, `..` collapsed) and must land under a declared root; a registry with projects but no `roots` is refused outright. The declared remote and base branch are checked against the repository actually on disk, because a path is not an identity. For a queued job the whole check runs **again at claim time**, so withdrawing a project takes effect for work already in the queue.

`sync_policy` controls the Git base admitted for a run:

- `offline` (default) never contacts the network and pins the configured local base branch, or the current `HEAD` when no branch is configured.
- `fetch` fetches only the configured remote-tracking base ref. The checkout is never reset or switched. A remote-ahead base is accepted and pinned; local/remote divergence is rejected.
- `require_up_to_date` performs the same fetch but rejects a local checkout that is behind or ahead of the remote, so it is suitable for strict unattended runs.

Every admitted run records the selected ref and SHA, remote identity, fetch timestamp, policy and outcome. Delivery is deliberately separate: the branch push helper re-checks that the recorded remote base has not moved and requires an explicit approval. No run silently pushes or changes the user's checkout.

## `config/platform.yaml`

```yaml
profile: balanced        # balanced | max -- selects config/presets/profiles/<name>.yaml

providers:
  quotas:
    codex_cli: {tokens: 8000000, window_hours: 5}
    claude_code: {tokens: 8000000, window_hours: 5}

routing:
  max_quota_ratio: 0.85
  min_success_rate: 0.6
  min_samples: 5
  window_hours: 24

workflow:
  mode: standard          # selects config/presets/workflow/<name>.yaml
  max_parallel: 2
  decompose: true
  max_correction_attempts: 1

context:
  mode: smart             # smart | full | minimal -- selects config/presets/context/<name>.yaml

# advanced:                # escape hatch, merged onto the resolved context preset
#   context: {min_similarity: 0.20, min_lift: 1.2, max_files: 20, max_context_chars: 20000}
```

Missing entirely, every field falls back to the value shown above — the same behavior as if the file were present with these exact contents. `profile`/`workflow.mode`/`context.mode` are validated against what preset files actually exist on disk at load time, so a typo is one `ConfigError`, before any worktree or branch is created, listing the names that are actually available.

Quota declarations are estimates: neither provider CLI reports a remaining balance, so pressure is derived from recorded telemetry against these limits. Getting a number wrong makes the pressure figure wrong and nothing else — no run is blocked by this file. Leave a provider out to report its consumption without a percentage.

The routing gates may skip a candidate but never rewrite the preset's declared order. If every candidate is gated, the first profile still runs, with a visible forced-fallback reason.

## Profile presets (`config/presets/profiles/*.yaml`)

Same shape as before, just resolved by name instead of a fixed path:

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

`profiles` is the `complex` policy and required fallback. Overrides accept only `routine`, `complex`, and `critical`. `profiles` and provider-neutral `effort` are the current form; legacy `provider`, `providers`, and `reasoning_effort` remain readable. Ambiguous duplicate fields, unsupported provider/effort pairs, and empty profile lists are configuration errors. See [Model routing policy](model-routing-policy.md).

Two presets ship: `balanced` (the default, calibrated policy) and `max` (every role's already-declared "critical" tier promoted to the unconditional base profile — no new tuning). Adding a preset is a data change: drop a new `<name>.yaml` file in the directory and point `profile:` at it — no code change needed, since preset names are discovered from what's on disk, not a hardcoded list.

## Workflow presets (`config/presets/workflow/*.yaml`)

```yaml
tasks:
  - id: architecture
    agent: architect
    depends_on: []
```

DAG shape only — `max_parallel`, `decompose`, and `max_correction_attempts` live in `config/platform.yaml`, not here, since they're operational knobs rather than architecture. Task IDs and roles must exist in the bounded workflow; the decomposer can prune tasks but cannot invent new ones. One preset ships: `standard`.

## Context presets (`config/presets/context/*.yaml`)

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

CLI providers use ranked path pointers by default because they can read the worktree; providers without disk access always receive full rendered excerpts regardless of `injection_mode`. Three presets ship: `smart` (default, every source on, pointers), `full` (every source on, excerpt text inlined), `minimal` (vector search only, tight budget). The three presets share the same calibrated relevance floors — only the retrieval toggles and budget differ; recalibrating the numbers themselves needs real measurement, not a config edit.

`config/platform.yaml`'s optional `advanced.context` block overrides individual fields of the resolved preset without editing the shipped file — for a project that needs different floors but doesn't want to author a whole new preset.

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

## Redaction and retention

The optional `security` block in `config/platform.yaml` defines deterministic secret redaction and retention: `redaction_patterns` adds project-independent regular expressions, while `retention` accepts `runs_days`, `calls_days`, `events_days`, `diffs_days` and `attachments_days`. A target may add `redaction_patterns` in `.ai-platform.yml`. Built-in token, bearer, JWT, private-key and credential-assignment formats are always covered.

Provider results, telemetry, audit notes and CLI job displays are redacted before they are persisted or shown. The durable job queue intentionally retains the original instruction and signed envelope: a detached worker must be able to execute the exact request after a restart. Its SQLite database is owner-only; do not export it unencrypted. This is an execution-at-rest boundary, not encrypted secret storage.

`ai-platform purge` applies retention on demand, and worker reconciliation applies the same policy automatically. It removes expired telemetry, completed jobs, settled or released reservations and old events; active jobs and held reservations are never purged. A retention value of `0` means retain that record class indefinitely. `delete_run`, `delete_session` and `delete_project` remove telemetry rows and leave non-sensitive tombstones for audit. Diff/attachment counts remain zero until those artifact stores are introduced. SQLite files are created with owner-only permissions; backups must preserve those permissions and be encrypted when leaving the workstation.

## Authentication and sensitive values

Subscription adapters rely on `codex login` and `claude auth login`. API adapters use provider environment credentials and separate billing. Never store secrets in YAML policy or prompts.

## Change discipline

A configuration change is a behavior change. Validate parsing, inspect every affected role/complexity route (`ai-platform route <role>`), run the test suite, and update the corresponding document. Consolidation of the previous six-file layout into `platform.yaml` plus presets shipped in [ADR-008](decisions/ADR-008-platform-config-and-presets.md), closing issue [#41](https://github.com/4nass/ai-platform/issues/41).
