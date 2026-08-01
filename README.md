# AI Software Engineering Platform

## Vision

AI Software Engineering Platform is **not a multi-tenant platform** — it's one engineer's own augmented software engineering copilot: a single-user system meant to automate and speed up the full software development lifecycle on the projects its owner works on.

The goal isn't to replace the developer, but to build a virtual team of specialized agents able to collaborate on a software project: requirement analysis, architecture design, code generation, technical review, testing, security, documentation, deployment.

It acts as a personal **Engineering Operating System**, coordinating several AI models (Claude, Codex, local models, etc.) while optimizing context and token usage. Because it's single-user by design, the stack stays deliberately minimal: no server processes to administer beyond what already runs locally (embedded vector index, SQLite, Markdown files) — no Redis, no PostgreSQL, no Kubernetes.

---

## Project Goals

### 1. An autonomous AI development team

The system should let complex tasks be delegated to several specialized agents.

Example: *"Add OAuth2 authentication with Microsoft Entra ID"* — the platform should automatically analyze the request, identify the impacted components, build an execution plan, assign tasks to the right agents, generate the changes, run the tests, perform a security review, and update the documentation.

### 2. Optimize AI model usage

The main problem with current assistants is context management. Sending an entire project to an LLM increases cost, reduces quality, and quickly hits context limits.

The platform introduces a **Context Engineering Layer** that supplies only the information actually needed. Instead of sending 5000 files / 500,000 lines, the system selects e.g. `AuthController.java`, `JwtService.java`, `SecurityConfig.java`, `architecture.md` — only what the task requires.

The detailed documentation lives in [`docs/`](docs/README.md): architecture, model-routing policy, configuration, operations, and testing.

---

## Current Implementation (Prototype 1)

What actually runs today, as opposed to the target vision described further below.

```text
                         User
                          |
                          v
                       Engine
                          |
        +-----------------+-----------------+
        |                                   |
   core/context/                    core/orchestrator/
   (RAG: chunking, embeddings,      (planner -> scheduler -> supervisor
    vector search, git diff,         -> git_ops -> test_runner)
    project memory)                          |
        |                                    v
        +----------------------------> providers/
                                             |
         +-------------+-------------+----------------+---------------+
         v             v             v                v
    claude_code    codex_cli     anthropic_api      openai_api
    (active,       (active,      (implemented,      (stub, not
     subprocess     subprocess    unexercised --      implemented)
     `claude -p     `codex exec   needs an API key)
     ...`)          --json`)
```

**Key design choice: the engine doesn't talk to a model directly.** It drives the `claude` CLI (Claude Code) as a subprocess, authenticated via the already-active subscription session (`claude auth login`) — no separate API billing. A provider abstraction (`providers/base.py`) makes the backend swappable: whichever provider runs, the contract is that the repo is already modified on disk by the time it returns, so the orchestrator stays agnostic to how the change was made.

**Engine vs. target repo.** The engine's own operating parameters — `config/*.yaml`, `prompts/*.md`, and the shared `telemetry.sqlite` — always live at the ai-platform install (`ENGINE_ROOT`), fixed regardless of what's being worked on. The repo actually being modified (`--repo`, default: the current directory) is a separate `target_root`: that's where git branches/worktrees are created, where the target's own test command runs, and where its context index (`.ai-platform/vector/`, `.ai-platform/graph.json`) and project memory (`memory/*.md`, if the target has one) live. They coincide when the engine runs on itself — the only mode that existed before `--repo` — but nothing in the target repo is assumed to look like ai-platform's own layout.

**How a run works:**
1. `core/context/manager.py` indexes the target repo (tree-sitter chunking for Python, section chunking for Markdown, whole-file otherwise) into a local, file-mode Qdrant vector index under `.ai-platform/`, then selects the chunks/files relevant to the request, plus the current git diff and the target's own `memory/*.md`.
2. `core/orchestrator/router.py` picks a provider for the role from the preference order in `config/agents.yaml`, skipping one that is over its declared quota or has been failing that role, and records the reason it chose. `core/orchestrator/scheduler.py` then builds and dispatches the task.
3. `core/orchestrator/planner.py` builds the task DAG declared in `config/workflow.yaml` (by default: architecture → backend/frontend → tests → security → documentation); a `decomposer` role call then prunes it to the subset of tasks the request actually needs, bridging dependencies through anything it drops. `core/orchestrator/supervisor.py` checks the run's `engine/<slug>` branch out in its **own integration worktree** — the target repo's checkout is never switched or written to, so a run can proceed while you keep working in it, and two runs can proceed at once. It then runs the (pruned) DAG — each task in its own git worktree, up to `max_parallel` tasks at once — merging every finished task's branch back with `--no-ff`. A task that writes outside its role's declared contract (`core/orchestrator/contracts.py`) is flagged, and so is one that writes to a path `.gitignore` hides — invisible to that same contract check and to the reviewer's diff otherwise, regardless of role. Neither reaches the run branch: a flagged stage's worktree is discarded, never merged. `core.hooksPath` is also redirected to an empty directory for the run's whole write-capable window, so a hook planted anywhere a stage can reach can't survive to fire on this run's own later git operations (`git_ops.disable_hooks`) — restored unconditionally once the run ends. A merge conflict blocks that branch for manual resolution rather than being auto-resolved. On success the integration worktree is removed and the branch remains as the deliverable; on failure the directory is kept and its path printed, since that's when the exact on-disk state answers questions the branch alone can't.

A dirty target working tree no longer blocks a run — nothing writes to it. It does get a warning, because the run branches from HEAD while the injected context still carries the uncommitted diff, so an agent can be shown code that isn't in what it's editing. A stage that raises unexpectedly — a role missing from `config/agents.yaml`, a provider throwing rather than returning a failure — fails only itself: the DAG degrades around it and its worktree is still reclaimed, rather than the exception killing every other in-flight stage and stranding the directory (issue #1).
4. Once the DAG finishes, the supervisor runs the target's test suite (`core/orchestrator/test_runner.py`, per `.ai-platform.yml` — see below) and sends the run's full diff to the `reviewer` role; its `VERDICT: PASS`/`FAIL` (`core/orchestrator/review.py`) gates the run's overall outcome alongside the tests.
5. If tests failed or the review verdict was FAIL — and every DAG stage otherwise completed — the `corrector` role gets the failure output and one bounded shot at fixing it (`config/workflow.yaml: max_correction_attempts`, default 1): fix, commit, re-run tests, re-run review, repeat until it passes or the attempts run out. A stage that itself failed, was skipped, or hit a merge conflict isn't something a corrector pass can retroactively complete, so those runs go straight to `needs attention` as before.

**Available roles** (`prompts/*.md` + `config/agents.yaml`): `backend`, `architect`, `frontend`, `reviewer`, `security`, `tests`, `documentation`, `corrector`, plus the internal `decomposer` role. Every role declares ordered Claude Code and Codex CLI profiles, including an explicit model and effort. The decomposer assigns the run one bounded complexity class (`routine`, `complex`, or `critical`); that class selects the role's profile list, while quota and recent-failure gates choose the first healthy profile within it. `reviewer` and `security` never get write access — read-only tools on `claude_code`, a `read-only` sandbox on `codex_cli` — because their output is a report, not a code change, and that is enforced by the CLI rather than by prompt instruction. See the [routing policy](docs/model-routing-policy.md) for the full matrix and rationale.

**Run it:**

```bash
uv run ai-platform run "Add a simple utility function"

# Against a different project (defaults to the current directory otherwise):
uv run ai-platform run "Add a simple utility function" --repo ~/code/some-other-project
```

**Declaring how a target repo validates a change.** `core/orchestrator/test_runner.py` never assumes `pytest`: it reads `test_command` (and optionally `test_timeout`, default 120s) from a `.ai-platform.yml` at the target repo's root. Absent entirely, tests are skipped — cleanly labeled `SKIPPED`, not silently treated as passing without saying so — rather than run a command that doesn't apply to that stack.

**Agent-written tests run before any review verdict exists** (issue #4) — that's inherent to "run the target's own test suite as the verification step," not something reordering fixes. What's fixable is that they used to run with the invoking user's full, unsandboxed privileges. By default, when [`bwrap`](https://github.com/containers/bubblewrap) is installed, they now run inside one: the host filesystem stays visible *read-only* (so the test command's own toolchain — `uv`, `npm`, `go`, whatever — keeps resolving normally; there's no per-target container image to know what a given repo needs), the target repo is bound read-write, and every namespace is unshared — no network, no writes anywhere else on the host. This is deliberately not the same guarantee a container would give; it closes the two highest-value risks (destructive writes elsewhere, network exfiltration) without adding a Docker/Podman dependency this project doesn't otherwise have. If `bwrap` isn't installed, tests still run — degrading loudly beats silently running unprotected — with a warning saying so.

**A run cannot grant itself permissions.** `.ai-platform.yml` is security policy — it decides whether the test command is sandboxed and what it is. Roles without an artifact contract (`backend`, `frontend`, `tests`, `corrector`) may write any tracked file, that one included, so it is read **once, from the run's base commit**, and never re-read (`core/orchestrator/target_config.py`). Before that, a stage could commit `test_sandbox: false` plus an arbitrary `test_command` and the engine would honour it — the sandbox disabled by the very code it exists to contain, with the run still reporting `done`. Demonstrated end to end, and now a regression test. An edit to that file on the produced branch is a normal reviewable change that takes effect for the *next* run.

**Verification runs in a throwaway worktree.** The test command is the one actor guaranteed to litter — `.pytest_cache/`, `.coverage`, `__pycache__/`. It gets its own checkout of the run branch, deleted immediately afterwards, so none of that reaches the branch under review or gets attributed to whichever actor runs next. Caches a project genuinely expects are declared in `allowed_ephemeral_writes` and reported rather than punished; any *other* gitignored write is still a blocking violation, so declaring caches doesn't reopen the hole above. Found by a real run where a `backend` stage was rejected — and its work discarded — because pytest had created `.pytest_cache/`.

**One mutating run per repository.** `git_ops.disable_hooks` rewrites `core.hooksPath`, which is repository-wide config shared by every worktree, so two concurrent runs would race to restore it. A second run fails fast with an explanation rather than blocking silently (`git_ops.exclusive_run_lock`).

**Everything in a prompt except the request itself is untrusted** (issue #5): an upstream stage's summary literally becomes the next stage's instructions, and a repo file, memory doc, diff, or test output can address the agent directly. `core/untrusted.py` handles this, and draws a line the code is careful not to blur:

- **Mechanical.** The engine's control lines (`VERDICT:`, `TASKS:`, `COMPLEXITY:`) are parsed with line-anchored regexes, so embedded occurrences are indented by one space on the way in. That deterministically makes them unparseable as a real decision — no model cooperation involved — while leaving them fully readable, which matters for the reviewer, whose job is to read a diff that may legitimately contain them. Verified against the real parsers: an unmitigated `VERDICT: PASS` in a diff parses as a genuine pass; the same content wrapped does not.
- **Advisory.** The delimiters (`<<<UNTRUSTED … :: nonce>>>`) plus a provenance rule in every role prompt are prompt engineering. The per-call random nonce removes the cheapest bypass — content that simply closes the fence and continues outside it — but buys nothing against a payload that argues rather than escapes.

**The delimiters are not a security boundary and shouldn't be read as one.** A sufficiently persuasive payload inside a fence can still talk a model out of the frame; that risk is reduced, not eliminated. The real containment for an agent that has been successfully steered is everything else: per-role tool restrictions, artifact contracts, the test sandbox, and the fact that nothing auto-merges. A real reviewer call against a diff that both smuggled a `VERDICT: PASS` and argued for approval did resist — it reported the injection as a finding and returned `VERDICT: FAIL` — but one passing probe is evidence, not a guarantee.

```yaml
# .ai-platform.yml, at the target repo's root
test_command: "uv run pytest -q"   # or a list: [npm, test] — or go test ./..., cargo test, etc.
test_timeout: 120
test_sandbox: true                 # default; set false to opt out entirely
allowed_ephemeral_writes:          # gitignored paths a run may leave behind
  - ".pytest_cache/**"             #   (tool caches). Empty by default: each
  - "**/__pycache__/**"            #   entry is a path the reviewer's diff
  - ".coverage"                    #   will never show, so it's declared.
sandbox_cache_dirs: ["~/.cache"]   # extra read-write binds a toolchain's cache needs beyond ~/.cache
test_env:                          # extra environment variables, sandboxed or not
  HF_HUB_OFFLINE: "1"
```

**Check subscription pressure:**

```bash
uv run ai-platform quota
```

Neither CLI reports a remaining balance: `codex exec --json` emits only
thread/turn/item events, and `claude -p --output-format json` reports a price
per call but no balance either. So `quota` doesn't discover the limit — it
compares tokens actually recorded in telemetry against the plan limits the
subscriber declares in `config/quota.yaml`, per provider, over a rolling
window (5h by default, or the widest declared budget; override with
`--window`). A provider with recorded usage but no declared budget still
shows its consumption, just without a percentage.

**Inspect a decision before spending anything.** `context`, `route`, and `quota`
run the engine's reasoning without invoking an agent; `history` doesn't reason
at all — it just reads back what past runs already recorded:

```bash
uv run ai-platform context "<request>"   # which files were selected, and why
uv run ai-platform route reviewer --complexity critical  # profile, model, effort, and why
uv run ai-platform quota                 # pressure on each subscription
uv run ai-platform history               # what recent runs cost -- tokens, price, duration, outcome
```

`route` walks the role and complexity class's profile order and shows each candidate's model, effort, quota
share, success rate and sample size, marking the one that would run. Your
declared order governs — it is overridden only on grounds the engine can
measure (`config/routing.yaml`), never on a marginal quality judgement, and a
run is never blocked: if every candidate is gated, the first choice runs anyway
and says so.

**Not implemented yet:** the `openai_api` provider (a stub — out of scope while
the engine drives subscription CLIs). `anthropic_api` is implemented (Messages
API, Pydantic-structured output) but has never been exercised: it needs an API
key, which is separate per-token billing rather than a subscription. Also
absent: MCP integration and any persisted `memory/*.md` content — the
files exist but are empty, and `core/memory/loader.py` globs only `memory/*.md`,
so `memory/adr/` is not loaded as memory either. Model and effort routing is
configured and recorded, but complexity is currently one run-wide class rather
than a separate classification for each stage. The decomposer still only
*prunes* the fixed DAG in `config/workflow.yaml`; it never composes a plan
tailored to the request. Correction attempts (see above) are recorded as
ordinary `calls` rows — `stage_id` `correction-1`, `correction-2`, ... — so
they already feed the existing quota/success-rate routing gates
(`core/orchestrator/router.py`) with no separate mechanism needed; there is
no dedicated policy that reacts to "this role/provider needed N correction
passes" beyond that.

---

## Target Architecture (Full Vision)

The rest of this document describes where the project is headed, not what's built. Treat every diagram below as a roadmap, not a status report.

```text
                         User
                          |
                          v

                  Engine Orchestrator

                          |
        +-----------------+-----------------+
        |                 |                 |
        v                 v                 v

   Task Planner      Scheduler        Supervisor


                          |
                          v

                Agent Execution Layer

        +-------------+-------------+-------------+
        |             |             |             |
        v             v             v             v

   Architect      Backend       DevOps       Security
   Agent          Agent        Agent        Agent


                          |
                          v

              Context Engineering Layer

        +-------------+-------------+-------------+
        |             |             |             |
        v             v             v             v

     Git Analysis  Code Graph     RAG        Memory


                          |
                          v

                    LLM Providers

        +-------------+-------------+-------------+
        |             |             |             |
        v             v             v             v

      Claude        Codex       Local Models   Others
```

### Main components

#### 1. Engine Orchestrator

The orchestration brain. It doesn't produce code directly. Responsibilities: understand the user request, break the work down, build a workflow, choose the agents, manage dependencies, control execution, supervise the results.

```
Engine
├── Planner
├── Scheduler
├── Supervisor
└── Workflow Engine
```

#### 2. Agent Layer

Each agent has a specialization.

- **Architect Agent** — technical analysis, architecture choices, diagram creation, technical decisions.
- **Backend Agent** — API development, business logic, database, backend integration.
- **Frontend Agent** — user interfaces, UI components, API integration.
- **DevOps Agent** — Docker/Podman, CI/CD, infrastructure as code. No Kubernetes: it's an admin layer a single-user copilot doesn't need.
- **Security Agent** — vulnerability analysis, OWASP, secrets, compliance.
- **Documentation Agent** — README, API docs, architecture, ADRs.

#### 3. Context Engineering Layer

The critical layer: decides what information reaches the agents.

```
User Request
      |
      v
Context Manager
      |
      +---- Git Diff
      +---- Code Graph
      +---- Vector Search
      +---- Project Memory
      +---- Documentation
      |
      v
Optimized Context
      |
      v
LLM Agent
```

#### 4. Code Intelligence Engine

Understands the project via Tree-sitter (already used for chunking today), AST analysis, git history, and a dependency graph built with **NetworkX** (`core/graph/builder.py`: AST imports + git co-changes + doc mentions, ranked via personalized PageRank). Goal: understand relationships between files, identify the impact of a change, avoid sending irrelevant code to the models.

Example: modifying `JwtService.java` — the system detects `AuthController`, `SecurityConfig`, and `TokenRepository` as impacted.

#### 5. Memory & History

Two different kinds of persisted knowledge, kept deliberately separate:

- **Markdown, for human-authored knowledge** — decisions and project rules a person wrote and can read/edit directly: `memory/architecture.md`, `memory/coding_rules.md`, `memory/business_rules.md`, `memory/roadmap.md`, `memory/adr/ADR-*.md`.
- **SQLite, for machine-generated history** — task runs, which agent/provider handled each one, and token/cost metrics per run. A local `.sqlite` file, no server, queryable directly (`sqlite3`, or `ai-platform history`) — implemented (`core/telemetry/store.py`). Lives at `ENGINE_ROOT`, shared across every `--repo` target (quota is a subscription resource, not a per-project one), with each run tagged by which target it touched (`runs.target_repo`); `ai-platform history` scopes to the resolved `--repo` by default.

```
memory/
├── architecture.md
├── coding_rules.md
├── business_rules.md
├── roadmap.md
└── adr/
    └── ADR-001-*.md

telemetry.sqlite   # runs(id, session_id, target_repo, request, branch, summary, engine_commit, started_at, finished_at, duration_ms, metadata)
                   # calls(id, run_id, stage_id, agent, provider, model, success, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, cost_usd, started_at, finished_at, duration_ms, provider_duration_ms, context_files, context_chars, routing_reason, context_reason, metadata)
```

#### 6. Vector Database / RAG

Semantic search over the project. Example: the question *"Where is authentication handled?"* resolves to `AuthenticationService`, `JWTProvider`, `SecurityConfig`, `OAuthController`. This stays an **embedded, file-mode Qdrant index** (`qdrant-client`, no server) permanently — not a "for now": a single-user copilot has no reason to run a separate vector database service.

#### 7. MCP Integration Layer

Model Context Protocol lets agents reach external tools: GitHub, Git, Podman, Database, Cloud. Agents would be able to create a git branch, analyze a repo, run tests, open a pull request, interact with infrastructure.

### Workflow management (example)

Request: *"Create a user API with authentication"*

```
Planner
 +-- Architecture
 +-- Backend
 +-- Tests
 +-- Security Review
 +-- Documentation

Scheduler
 +-- Claude    -> Architecture
 +-- Codex     -> Backend
 +-- Codex     -> Tests
 +-- Claude    -> Security
 +-- Claude    -> Documentation

Supervisor
 +-- Validation
 +-- Correction
 +-- Merge
```

### Token optimization

1. **Smart context selection** — never send the whole project.
2. **Search before generation** — question → search → minimal context → LLM.
3. **Specialized agents** — an agent only receives what its role needs.
4. **Caching** — frequent results are kept.
5. **Per-agent budget** — e.g. `architect: max_tokens: 12000`, `backend: max_tokens: 10000`, `reviewer: max_tokens: 8000`.

### Target infrastructure

```
Windows
 |
 +-- WSL2 Ubuntu
       |
       +-- Python
       +-- uv
       +-- Claude Code
       +-- Codex CLI
       +-- Engine
```

This is the full target, not just what prototype 1 needs — deliberately: no Redis, no PostgreSQL, no Kubernetes, ever. The vector index (Qdrant), the graph (NetworkX), and the run history (SQLite) are all local, embedded, file-based — no service to start or administer. Podman shows up only as a tool the DevOps Agent can call for the user's own project builds, not as infrastructure this platform depends on.

---

## Roadmap

### Phase 1 - Foundation
- [x] Project structure
- [x] Python configuration (`uv`, `pyproject.toml`)
- [x] Model/provider configuration (`config/agents.yaml`, `config/models.yaml`)
- [x] CLI (`uv run ai-platform run ...`)

### Phase 2 - Context Engine
- [x] Git diff analysis
- [x] Code parsing with Tree-sitter
- [x] Dependency graph with NetworkX (`core/graph/builder.py`: AST imports + git co-changes + doc mentions, ranked via personalized PageRank)
- [x] Vector search (embedded Qdrant + sentence-transformers)
- [x] Memory Manager (loads `memory/*.md`; files still empty)
- [x] SQLite run history (`core/telemetry/store.py`; `ai-platform history`)

### Phase 3 - Agent System
- [x] Architect Agent (prompt written, not yet exercised end-to-end)
- [x] Backend Agent (implemented and verified end-to-end)
- [x] Reviewer Agent (prompt + read-only tool restriction, not yet exercised)
- [x] Security Agent (prompt + read-only tool restriction, not yet exercised)
- [x] Documentation Agent (prompt written, not yet exercised)
- [x] Frontend / Tests Agents (prompts written, not yet exercised)

### Phase 4 - Engine
- [x] Planner (task DAG from `config/workflow.yaml`, pruned per request by the `decomposer` role)
- [x] Scheduler (concurrent — up to `max_parallel` tasks at once, each in its own git worktree)
- [x] Supervisor (branch, run task DAG in per-task worktrees, merge `--no-ff`, test, review gate, report)
- [x] Workflow Engine (task DAG declared in `config/workflow.yaml`, per-task git worktrees, `decomposer`-pruned per request)
- [x] `run --dry-run` flag: print the planned workflow and the decomposer's
      selected tasks without invoking any workflow-task agent (see
      `memory/adr/ADR-001-cli-dry-run-flag.md`)
- [x] `--repo` flag: operate on any git repo, not just the ai-platform repo
      itself — engine config/prompts/telemetry stay fixed at the install,
      the target repo's own `.ai-platform.yml` declares its test command
- [x] Bounded test/review correction loop: on failure, feed the corrector
      role the failure output for up to `max_correction_attempts` fix passes
      before giving up as `needs attention`

### Phase 5 - Automation
- [ ] MCP
- [ ] CI/CD
- [ ] Automatic Pull Requests
- [ ] Automatic tests beyond the target repo's own suite
- [ ] Security scanning

---

## Project Principles

- **Modularity** — every component should be replaceable. Swapping Claude for a local model shouldn't require rewriting the system.
- **Context First** — an agent's quality mostly depends on the quality of the context it's given.
- **Human in the Loop** — critical decisions stay validated by a human (changes always land on a dedicated branch, never auto-merged).
- **Security by Design** — every code generation must be reviewable and controllable.

## Final Vision

A personal copilot able to turn an idea into working software by orchestrating a small team of AI agents on the user's own projects, while keeping code control, cost control, decision traceability, security, and software quality — with a stack minimal enough that one person can run and understand every part of it.
