# Technology stack

Versions below are minimum constraints from `pyproject.toml`; `uv.lock` is the reproducible resolution.

## Runtime and packaging

| Technology | Purpose | Operational note |
|---|---|---|
| Python 3.11+ | Engine runtime | Type hints, subprocess orchestration, and SQLite |
| uv / uv_build | Dependency sync, commands, packaging | Use `uv sync --frozen` and `uv run ...` |
| Typer 0.27+ | CLI surface | Exposes the `ai-platform` executable |
| Rich 15+ | Terminal reports | Presentation only; core results remain structured |
| Pydantic 2.13+ | Configuration and data contracts | Rejects malformed profiles and results |
| PyYAML 6+ | Human-maintained policy files | YAML input is validated after parsing |

## Context engineering

| Technology | Purpose | Choice and constraints |
|---|---|---|
| Qdrant Client 1.18+ | Local vector collection | File-backed target-local storage; cosine similarity |
| sentence-transformers 5.6+ | Embeddings | `all-MiniLM-L6-v2`, 384 dimensions |
| Tree-sitter 0.26+ | Syntax-aware source chunking | Python grammar currently installed |
| tree-sitter-python 0.23+ | Python parser | Extracts top-level function/class chunks |
| NetworkX 3.6+ | Dependency/co-change graph | `MultiDiGraph` plus personalized PageRank |
| GitPython 3.1+ | Repository metadata and changes | Low-level worktree operations may still use Git subprocesses |

`langchain` is declared but is not on the current production path. Removing it or giving it a clear responsibility is tracked by [#10](https://github.com/4nass/ai-platform/issues/10).

## Providers

| Integration | State | Billing/authentication |
|---|---|---|
| Claude Code CLI | Active | Existing local Claude subscription session |
| Codex CLI | Active | Existing local Codex session |
| Anthropic Python SDK 0.115+ | Implemented but not the default path | Separate API credentials and billing |
| OpenAI API adapter | Stub | Not a delivered provider |
| Local model adapter | Planned | Tracked by [#37](https://github.com/4nass/ai-platform/issues/37) |

The CLI adapters are subprocess integrations. Their stdout is parsed into normalized provider results; filesystem changes remain the authoritative artifact.

## Storage and operating-system facilities

| Technology | Purpose | Boundary |
|---|---|---|
| SQLite | Telemetry and durable job queue | Local single-owner operation; WAL for telemetry, separate `jobs.sqlite` |
| Git branches/worktrees | Snapshot, isolation, delivery | Branch is the durable code artifact |
| `flock` | Mutating-run serialization | Same-machine only |
| Bubblewrap | Target-test sandbox | Linux optional dependency; warning fallback |
| File system | Vector index, graph cache, worktrees | Retention and encryption are not yet centralized |

## Development and verification

| Technology | Purpose |
|---|---|
| pytest 8.3+ | Unit, contract, and orchestration tests |
| Real Git temporary repositories | Worktree and isolation integration tests |
| Fake provider adapters | Deterministic routing and workflow tests |

## Selection rationale

The stack is deliberately local and inspectable: Git expresses code state, SQLite expresses local structured history, Qdrant provides semantic retrieval without a service, and provider CLIs reuse personal subscriptions. This is appropriate for a single-user workstation. A remotely reachable service requires authentication, service supervision, database migration discipline, secrets isolation, and retention before the same components can be safely exposed.
