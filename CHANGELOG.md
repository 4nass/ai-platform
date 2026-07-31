# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- CLI (`uv run ai-platform run "<request>"`) driving Claude Code as the active provider, plus a `--dry-run` flag that prints the planned workflow and task selection without invoking any workflow-task agent (`memory/adr/ADR-001-cli-dry-run-flag.md`).
- Multi-stage workflow engine: a task DAG (`config/workflow.yaml`) executed per-task in isolated git worktrees (`core/orchestrator/git_ops.py`), merged back with `--no-ff`, with a Task Decomposer role that prunes the plan per request.
- Context Engineering Layer: tree-sitter/section chunking, an embedded Qdrant vector index, and a git-history + PageRank dependency graph (`core/graph/builder.py`, combining AST imports, git co-changes, and doc mentions).
- Per-role artifact contracts (`core/orchestrator/contracts.py`): a post-hoc check on which files each role is allowed to touch, backstopping the CLI tool restrictions.
- Review gate: every run's diff is sent to a `reviewer` role, and its PASS/FAIL verdict gates completion (`core/orchestrator/review.py`).
- Run telemetry (`core/telemetry/store.py`): per-call and per-run cost/token accounting, config snapshots, and routing/context decision logs.
- Two working providers: `claude_code` (`claude -p`) and `codex_cli` (`codex exec --json`), both subprocess CLIs that edit files themselves. `anthropic_api` (Messages API, Pydantic-structured output) is implemented but unexercised — it needs an API key rather than a subscription.
- Provider routing (`core/orchestrator/router.py`): each role declares an ordered preference in `config/agents.yaml`, and the router skips a candidate only on measurable grounds — quota pressure, or a success rate below the floor on a large enough sample (`config/routing.yaml`). Every choice is recorded in the `routing_reason` column, and `ai-platform route` explains one without spending a token.
- Subscription quota accounting (`core/telemetry/quota.py`, `config/quota.yaml`): neither CLI reports a remaining balance, so declared budgets are compared against tokens actually recorded. `ai-platform quota` shows the pressure per provider.
- `ai-platform context` and `ai-platform route`: inspect the file selection and the provider choice without invoking an agent.
- `ai-platform history`: shows recent runs' cost, tokens, duration, and outcome from recorded telemetry.

### Changed

- Context selection now scores and gates candidates instead of injecting whatever retrieval returned: vector hits require a similarity floor, graph expansion is gated by lift (personalized vs. background PageRank), and every candidate's keep/drop decision is recorded (`core/context/selection.py`).
- Rendered context (not just file paths) is now actually sent to providers; each adapter declares `READS_FILES` to choose between full content and a ranked pointer map.
- Token accounting sums cached and uncached input tokens instead of reporting only the uncached remainder, so reported cost reflects the full prompt (`providers/anthropic_api/adapter.py`, `core/orchestrator/supervisor.py`).
- Reporting leads with tokens, duration and outcome rather than dollars. With two flat-rate subscriptions a per-call price measures nothing actionable; cost is still recorded where a provider volunteers one, but nothing decides on it.
- `providers/base.TokenUsage` now states the platform's token convention explicitly, and each adapter converts into it. Anthropic reports the cached count *disjoint* from the input count; OpenAI reports it as a *subset*. Summing both shapes the same way would have reported 25,002 tokens for a 13,994-token Codex prompt.
- Documentation's artifact contract broadened from `README.md`/`memory/*.md` to any `*.md` file, after a real run needed to create `CONTRIBUTING.md` at the repo root (`core/orchestrator/contracts.py`).

### Fixed

- `Plan.prune()` now bridges dependencies through pruned nodes instead of dropping them.
- `--dry-run` no longer requires a clean worktree.
- Routing reads telemetry from the main repo rather than the task's throwaway git worktree. It had been deciding from an empty database — every DAG stage cold-starting forever — and leaving a stray `telemetry.sqlite` inside the worktree for that stage's own commit to pick up (`core/orchestrator/scheduler.py`).
- Task branches are uniquified like run branches. A stage that failed kept its branch on purpose, so its partial work stays inspectable, but the next run of the same request then died inside `git worktree add` before reaching its provider (`core/orchestrator/git_ops.py`). The failure path now also says where that partial work went.
- A failed provider call records why it failed. The table previously held `success = 0` and nothing else, which measures a failure rate without explaining one; the message is queryable via `json_extract(metadata, '$.error')`.

### Security

- Review-gate bypass: `parse_verdict()` matched the first `VERDICT:` occurrence anywhere in the reviewer's response, including one quoted from the diff under review; now anchored to the last line-start match, and fails closed on a malformed verdict (`core/orchestrator/review.py`).
- Same first-match parsing flaw in the Task Decomposer, where the prompt's own example could outrank the real decision.
- Path traversal in the Anthropic API provider: model-supplied file paths could escape the repo via `../` traversal, an absolute path, or a symlink; writes are now resolved and validated against the repo root up front, for the whole plan, before anything is written (`providers/anthropic_api/adapter.py`).
