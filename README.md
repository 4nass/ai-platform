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

---

## Current Implementation (Prototype 1)

What actually runs today, as opposed to the target vision described further below.

```text
                         User
                          |
                          v
                       Hermes
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
                        +--------------------+--------------------+
                        v                    v                    v
                  claude_code           anthropic_api          codex_cli /
                  (active,             (Anthropic API,          openai_api
                   subprocess          Pydantic-structured      (stubs, not
                   `claude -p ...`)    output, writes            implemented)
                                       files itself)
```

**Key design choice: Hermes doesn't talk to a model directly.** It drives the `claude` CLI (Claude Code) as a subprocess, authenticated via the already-active subscription session (`claude auth login`) — no separate API billing. A provider abstraction (`providers/base.py`) makes the backend swappable: whichever provider runs, the contract is that the repo is already modified on disk by the time it returns, so the orchestrator stays agnostic to how the change was made.

**How a run works:**
1. `core/context/manager.py` indexes the repo (tree-sitter chunking for Python, section chunking for Markdown, whole-file otherwise) into a local, file-mode Qdrant vector index, then selects the chunks/files relevant to the request, plus the current git diff and `memory/*.md`.
2. `core/orchestrator/scheduler.py` resolves which provider to use for the requested role from `config/agents.yaml`, and builds the task.
3. `core/orchestrator/supervisor.py` creates an isolated `hermes/<slug>` git branch *before* running the provider (a CLI provider edits files live), runs it, commits whatever changed, then runs the test suite and reports PASS/FAIL.

**Available roles** (`prompts/*.md` + `config/agents.yaml`, all routed to `claude_code` today): `backend`, `architect`, `frontend`, `reviewer`, `security`, `tests`, `documentation`. `reviewer` and `security` are restricted to read-only tools (`Read,Grep,Glob`) — their output is a report, not a code change, enforced at the tool level, not just by prompt instruction.

**Run it:**

```bash
uv run ai-platform run "Add a simple utility function" --agent backend
```

**Not implemented yet:** `codex_cli` and `openai_api` providers (stubs only — no verified CLI/API syntax), the code graph (`use_graph` in `config/context.yaml` is acknowledged but ignored), MCP integration, multi-step task planning (the planner currently produces a single task), and any persisted `memory/*.md` content (the files exist but are empty).

---

## Target Architecture (Full Vision)

The rest of this document describes where the project is headed, not what's built. Treat every diagram below as a roadmap, not a status report.

```text
                         User
                          |
                          v

                  Hermes Orchestrator

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

#### 1. Hermes Orchestrator

The orchestration brain. It doesn't produce code directly. Responsibilities: understand the user request, break the work down, build a workflow, choose the agents, manage dependencies, control execution, supervise the results.

```
Hermes
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

Understands the project via Tree-sitter (already used for chunking today), AST analysis, git history, and a dependency graph built with **NetworkX** (already a dependency, not yet wired in — see Roadmap). Goal: understand relationships between files, identify the impact of a change, avoid sending irrelevant code to the models.

Example: modifying `JwtService.java` — the system detects `AuthController`, `SecurityConfig`, and `TokenRepository` as impacted.

#### 5. Memory & History

Two different kinds of persisted knowledge, kept deliberately separate:

- **Markdown, for human-authored knowledge** — decisions and project rules a person wrote and can read/edit directly: `memory/architecture.md`, `memory/coding_rules.md`, `memory/business_rules.md`, `memory/roadmap.md`, `memory/adr/ADR-*.md`.
- **SQLite, for machine-generated history** — task runs, which agent/provider handled each one, and token/cost metrics per run. A local `.sqlite` file, no server, queryable directly (`sqlite3`, or a `history` CLI command later). Not yet implemented — see Roadmap.

```
memory/
├── architecture.md
├── coding_rules.md
├── business_rules.md
├── roadmap.md
└── adr/
    └── ADR-001-*.md

hermes.sqlite   # runs(id, agent, provider, request, branch, success, tokens, cost, ts)
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
       +-- Hermes
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
- [ ] Dependency graph with NetworkX (`use_graph` acknowledged, not implemented)
- [x] Vector search (embedded Qdrant + sentence-transformers)
- [x] Memory Manager (loads `memory/*.md`; files still empty)
- [ ] SQLite run history (task, agent, provider, tokens, cost per run)

### Phase 3 - Agent System
- [x] Architect Agent (prompt written, not yet exercised end-to-end)
- [x] Backend Agent (implemented and verified end-to-end)
- [x] Reviewer Agent (prompt + read-only tool restriction, not yet exercised)
- [x] Security Agent (prompt + read-only tool restriction, not yet exercised)
- [x] Documentation Agent (prompt written, not yet exercised)
- [x] Frontend / Tests Agents (prompts written, not yet exercised)

### Phase 4 - Hermes
- [x] Planner (single-task only — no real breakdown yet)
- [x] Scheduler (synchronous, no parallelism)
- [x] Supervisor (branch, run, commit, test, report)
- [ ] Workflow Engine (multi-step plans)
- [x] `run --dry-run` flag: print the planned workflow and the decomposer's
      selected tasks without invoking any workflow-task agent (see
      `memory/adr/ADR-001-cli-dry-run-flag.md`)

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
