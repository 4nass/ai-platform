# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- CLI (`uv run ai-platform run "<request>"`) driving Claude Code as the active provider, plus a `--dry-run` flag that prints the planned workflow and task selection without invoking any workflow-task agent (`memory/adr/ADR-001-cli-dry-run-flag.md`).
- Multi-stage workflow engine: a task DAG (`config/workflow.yaml`) executed per-task in isolated git worktrees (`core/orchestrator/git_ops.py`), merged back with `--no-ff`, with a Task Decomposer role that prunes the plan per request.
- Context Engineering Layer: tree-sitter/section chunking, an embedded Qdrant vector index, and a git-history + PageRank dependency graph (`core/graph/git_deps.py`).
- Per-role artifact contracts (`core/orchestrator/contracts.py`): a post-hoc check on which files each role is allowed to touch, backstopping the CLI tool restrictions.
- Review gate: every run's diff is sent to a `reviewer` role, and its PASS/FAIL verdict gates completion (`core/orchestrator/review.py`).
- Run telemetry (`core/telemetry/store.py`): per-call and per-run cost/token accounting, config snapshots, and routing/context decision logs.
- Two providers: `claude_code` (CLI subprocess, disk access, active) and `anthropic_api` (Messages API with Pydantic-structured output, writes files itself).

### Changed

- Context selection now scores and gates candidates instead of injecting whatever retrieval returned: vector hits require a similarity floor, graph expansion is gated by lift (personalized vs. background PageRank), and every candidate's keep/drop decision is recorded (`core/context/selection.py`).
- Rendered context (not just file paths) is now actually sent to providers; each adapter declares `READS_FILES` to choose between full content and a ranked pointer map.
- Token accounting sums cached and uncached input tokens instead of reporting only the uncached remainder, so reported cost reflects the full prompt (`providers/anthropic_api/adapter.py`, `core/orchestrator/scheduler.py`).
- Documentation's artifact contract broadened from `README.md`/`memory/*.md` to any `*.md` file, after a real run needed to create `CONTRIBUTING.md` at the repo root (`core/orchestrator/contracts.py`).

### Fixed

- `Plan.prune()` now bridges dependencies through pruned nodes instead of dropping them.
- `--dry-run` no longer requires a clean worktree.

### Security

- Review-gate bypass: `parse_verdict()` matched the first `VERDICT:` occurrence anywhere in the reviewer's response, including one quoted from the diff under review; now anchored to the last line-start match, and fails closed on a malformed verdict (`core/orchestrator/review.py`).
- Same first-match parsing flaw in the Task Decomposer, where the prompt's own example could outrank the real decision.
- Path traversal in the Anthropic API provider: model-supplied file paths could escape the repo via `../` traversal, an absolute path, or a symlink; writes are now resolved and validated against the repo root up front, for the whole plan, before anything is written (`providers/anthropic_api/adapter.py`).
