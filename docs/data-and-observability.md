# Data, telemetry, and budgets

## Storage overview

The platform uses different stores for different lifecycles.

| Store | Location | Purpose | Status |
|---|---|---|---|
| `telemetry.sqlite` | engine root | Append-oriented run and provider analytics | Delivered |
| `jobs.sqlite` | engine root | Mutable asynchronous job lifecycle | Delivered |
| Qdrant `vector/` | target `.ai-platform/` | Semantic chunks and embeddings | Delivered |
| `graph.json` | target `.ai-platform/` | Dependency/co-change graph cache | Delivered |
| Git branches | target repository | Durable code delivery artifact | Delivered |

Telemetry and jobs should remain separate: analytical call history has different mutation, retention, locking, and recovery semantics from a queue.

## Telemetry

SQLite runs in WAL mode and uses short-lived connections. The main entities are runs and provider calls.

Recorded data includes:

- run identifier, target repository, request, stage, and outcome;
- role, provider, requested model, requested effort, and complexity;
- effective model when reported by the provider;
- input/output token counts and estimated or provider-reported cost;
- duration, timestamps, session identifier, and error category;
- routing decision, quota pressure, and candidate rejection reasons;
- context selection and policy metadata needed to interpret the result.

Telemetry is shared across target repositories because provider subscription pressure is shared. User-facing history should scope by target by default.

## Quota pressure

`config/platform.yaml`'s `providers.quotas` declares token allowances and windows. The current shipped values are 8,000,000 tokens over 5 hours for each CLI provider. The router compares recorded consumption with these declarations.

This number is an estimate because subscription CLIs do not expose authoritative remaining balance. It is useful for failover but cannot be called a budget guarantee. A provider omitted from the file can still be measured without a percentage.

## Hard budgets

Remote unattended execution requires hard admission and runtime budgets independent of routing:

- maximum input and output tokens;
- maximum provider calls;
- maximum elapsed time;
- maximum corrections/retries;
- optional currency ceiling for API-backed providers;
- explicit behavior when estimates are unavailable.

The admission decision must occur before claiming expensive work, and the worker must stop when a hard limit is reached. This is tracked by [#27](https://github.com/4nass/ai-platform/issues/27).

## Durable jobs

`core/jobs/` (issue [#24](https://github.com/4nass/ai-platform/issues/24)) separates a job's state — `queued`, `running`, `waiting_approval`, `succeeded`, `failed`, `cancelled`, `interrupted` — from the analytical `runs` table above, in its own `jobs.sqlite`. `ai-platform submit` persists the request and returns a job id before any provider is contacted; `status`, `jobs`, `cancel`, and `work` read and drive the queue from any process.

Delivered:

- atomic claim by one worker (the guard lives in the claiming `UPDATE`, not a read-then-write, so two racing workers resolve in SQLite);
- a background heartbeat thread and stale-worker reconciliation, run on every read path (`jobs`, `status`, `work`) rather than requiring an operator to trigger it;
- an append-only `job_events` table recording every transition, which is what answers "why is this interrupted" once the process that interrupted it is gone;
- cancellation for jobs that have not started executing (a `running` job cannot yet be stopped mid-run — that is [#29](https://github.com/4nass/ai-platform/issues/29));
- progress fields for run id, base ref/sha, branch, integration worktree, stage, and attempt, written as the run reaches them via a `progress` callback into `supervisor.run`;
- a job whose target repository is locked by another run returns to `queued` rather than failing — one mutating run per repo is a scheduling conflict, not a defect in the job;
- terminal state and full event history queryable after a process restart.

Interrupted, not resumed: a job whose worker dies is reconciled to `interrupted`, keeping everything above so its already-committed work is inspectable, but the remaining DAG stages are not retried automatically — that needs per-stage checkpointing that does not exist yet.

Verifying this against a real SIGKILL'd worker surfaced a genuine defect, since fixed: `disable_hooks` restores the target's `core.hooksPath` in a `finally`, which does not run on a hard kill, leaving the user's own git hooks silently disabled indefinitely. The previous value is now saved in the repo's own config before the swap, and reconciliation repairs it.

## Retention and privacy

There is no complete retention policy today. Before remote use, define per-store retention, deletion, backup, encryption, and redaction. Requests, context metadata, provider output, repository-derived embeddings, and error logs can all reveal confidential source information.

Schema changes should use explicit migrations before the databases become a supervised long-running service.
