# Data, telemetry, and budgets

## Storage overview

The platform uses different stores for different lifecycles.

| Store | Location | Purpose | Status |
|---|---|---|---|
| `telemetry.sqlite` | engine root | Append-oriented run and provider analytics | Delivered |
| `jobs.sqlite` | engine root | Job lifecycle, budget reservations, approvals | Delivered |
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

Delivered in `core/jobs/budget.py` (issue [#27](https://github.com/4nass/ai-platform/issues/27)). Limits are declared per class in `config/platform.yaml` (`max_run_tokens`, `max_stage_tokens`, `max_run_calls`, `max_window_tokens` over a rolling window) and a project selects its class in `config/projects.yaml`, so the allowlist says which budget a repository belongs to without restating the amounts.

**Reservations, not just accounting.** Checking consumption after each call cannot bound anything: by the time the number moves the tokens are spent, and two concurrent runs each see the other's spending only once it is over. Capacity is reserved before dispatch and reconciled with the real figure afterwards, and admission sums held reservations as well as settled ones — which is what stops two jobs each admitting a call the budget can afford once.

**One gate, structurally.** `PROVIDERS[...]` is dispatched on a single line of `scheduler.run_task` and nowhere else in the engine, so "no adapter can bypass the budget gate" is a property of the shape of the code rather than a rule every adapter has to remember.

**Failure is not free.** A failed call is *settled* at its real cost, not released: a provider that errored after processing a 200k-token prompt spent those tokens, and making failures free is backwards for a loop that retries them. Only a call that never reached a provider is released. A provider that reports no usage settles at the estimate, since silence is not evidence of being free.

**Modes.** `soft` records and reports without blocking (the interactive default). `strict` refuses the call and moves the job to `waiting_approval` — paused, not failed, because what stopped it is a policy ceiling and the answer is a human decision. `local_fallback` is selectable and currently equivalent to `strict` in effect: no local adapter exists yet ([#37](https://github.com/4nass/ai-platform/issues/37)), so it waits rather than quietly spending on a paid provider.

**Estimates are labelled as estimates.** No local tokenizer covers a subscription CLI, so the pre-call figure is a documented character heuristic plus a fixed output allowance, deliberately biased to over-reserve — an over-large reservation delays a call, an under-large one permits a call the budget could not afford, and only the first is recoverable. The run report shows reserved *and* consumed; the gap between them is the only number that says whether the heuristic is calibrated for the work this engine actually does.

Reservations held by a crashed run are reclaimed on age by the same reconciliation that marks jobs interrupted. Left held they would shrink every later run's window forever — a budget that tightens itself every time something crashes.

## Durable jobs

`core/jobs/` (issue [#24](https://github.com/4nass/ai-platform/issues/24)) separates a job's state — `queued`, `running`, `waiting_approval`, `succeeded`, `failed`, `cancelled`, `interrupted` — from the analytical `runs` table above, in its own `jobs.sqlite`. `ai-platform submit` persists the request and returns a job id before any provider is contacted; `status`, `jobs`, `cancel`, and `work` read and drive the queue from any process.

Delivered:

- atomic claim by one worker (the guard lives in the claiming `UPDATE`, not a read-then-write, so two racing workers resolve in SQLite);
- a background heartbeat thread and stale-worker reconciliation, run on every read path (`jobs`, `status`, `work`) rather than requiring an operator to trigger it;
- an append-only `job_events` table recording every transition — and every refusal, including a submission rejected for reusing an idempotency key with different content, which is committed explicitly so the exception reporting it cannot roll it back;
- idempotent submission: a `Principal` and a structured envelope are stored alongside the request, and the key derived from the transport's own identifiers carries a unique index, so a redelivered message returns the original job id instead of starting a second run;
- cancellation for jobs that have not started executing (a `running` job cannot yet be stopped mid-run — that is [#29](https://github.com/4nass/ai-platform/issues/29));
- resumption of an interrupted job onto its own branch, keeping every stage it merged (`interrupted` is the one terminal state that can be left, and only to `queued`, and only via `resume`);
- progress fields for run id, base ref/sha, branch, integration worktree, stage, and attempt, written as the run reaches them via a `progress` callback into `supervisor.run`;
- a job whose target repository is locked by another run returns to `queued` rather than failing — one mutating run per repo is a scheduling conflict, not a defect in the job;
- terminal state and full event history queryable after a process restart.

Interrupted, then resumable: a job whose worker dies is reconciled to `interrupted`, keeping everything above, and `ai-platform resume <id>` puts the same job back in the queue to continue on its own branch. `core/orchestrator/checkpoint.py` records each stage as it merges — in the integration worktree's own git directory, where `git add -A` cannot sweep it onto the branch and where it dies with the worktree a successful run removes. A resumed run adopts that worktree, restores the base commit, complexity and pruned task set, and skips what is already merged; verification and review are re-run, since they are one provider call each against a tree that has moved. The checkpoint is written *after* a merge, never before, so it can only under-claim: a crash in that gap costs one repeated stage, where the reverse would silently drop work off the branch.

Resuming is never automatic. A worker that re-queued crashed jobs by itself would retry, in a loop, exactly the runs most likely to kill the next worker too.

Verifying this against a real SIGKILL'd worker surfaced a genuine defect, since fixed: `disable_hooks` restores the target's `core.hooksPath` in a `finally`, which does not run on a hard kill, leaving the user's own git hooks silently disabled indefinitely. The previous value is now saved in the repo's own config before the swap, reconciliation repairs it, and — because reconciliation only runs on the jobs path, and because reading a leaked value as "what the user had" made the next clean run restore the *neutralization* and drop the saved key — `disable_hooks` also repairs one on entry. Without that second half the leak was permanent and unrepairable after one further run, which was measured.

## Retention and privacy

There is no complete retention policy today. Before remote use, define per-store retention, deletion, backup, encryption, and redaction. Requests, context metadata, provider output, repository-derived embeddings, and error logs can all reveal confidential source information.

Schema changes should use explicit migrations before the databases become a supervised long-running service.
