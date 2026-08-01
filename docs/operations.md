# Operations

## Install and inspect

Use the locked environment:

```bash
uv sync --frozen
uv run ai-platform --help
```

Inspect decisions without spending a provider call:

```bash
uv run ai-platform context "Add an OAuth callback"
uv run ai-platform route architect --complexity routine
uv run ai-platform route architect --complexity complex
uv run ai-platform route architect --complexity critical
uv run ai-platform quota
uv run ai-platform history
```

A route inspection shows the selected provider, requested model, effort, quota share, success history, and rejection reasons for alternatives.

## Run modes

```bash
# Dogfood against this repository
uv run ai-platform run "Update model routing"

# Operate on another repository
uv run ai-platform run "Update model routing" --repo /path/to/project

# Inspect the planned execution without writable agent stages
uv run ai-platform run "Update model routing" --dry-run
```

The run branch uses the `engine/<slug>` namespace in the target repository. The human operator should inspect the final report and Git history before merging the result onward.

## Telemetry

Each provider call records:

- role, stage, provider, requested model, and requested effort;
- run complexity;
- duration, outcome, token usage, and reported price when available;
- provider session identifier when available;
- `effective_model` in metadata when the provider reports the model it actually used.

Requested and effective model are intentionally separate. A provider may alias or fall back from the requested identifier; audits should prefer `effective_model` when present and preserve the original request for reproducibility.

Telemetry is shared at the engine root so quota pressure reflects all target repositories. History views are scoped to the selected target by default.

## Quota interpretation

Quota is a local pressure estimate over a rolling window, not the provider's authoritative subscription balance. A profile may be gated because its provider is above the configured share. Exact-profile recent failures can gate one model/effort combination without condemning every profile on that provider.

When all candidates are gated, the engine runs the first declared profile and makes the reason visible. This preserves progress while keeping policy deterministic.

## Troubleshooting

### The wrong model appears selected

Run `ai-platform route <role> --complexity <class>` and inspect:

1. the profile order in `config/agents.yaml`;
2. provider quota pressure;
3. exact-profile recent failure samples;
4. whether the run complexity was parsed or defaulted to `complex`.

### Claude rejects effort or model

Check `claude --version`, update Claude Code, and compare the configured identifier with the official model configuration documentation. The shipped Pro policy does not select `ultracode`; if a custom policy enables it, use a recent compatible release.

### Codex rejects effort or model

Check `codex --version` and current OpenAI model guidance. Model availability can depend on the installed client, account, and rollout.

### A stage changed forbidden files

Inspect the preserved worktree and role contract. Do not weaken the contract merely to accept an unrelated edit; split the request or assign the correct role.

### Tests are skipped

Add `.ai-platform.yml` with the target's actual validation command. Skipped is explicit and is not equivalent to a passing test suite.

### A temporary worktree remains

This normally means a merge conflict or failed cleanup. Confirm that the path belongs to the target repository and contains work worth preserving before removing it manually.
