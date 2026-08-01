# ADR-009: Resumable runs, checkpointed in git rather than in the queue

- Status: Accepted
- Date: 2026-08-02

## Context

[#24](https://github.com/4nass/ai-platform/issues/24) delivered durable jobs and crash detection: a worker that dies is reconciled to `interrupted`, keeping its base commit, branch, stage and integration worktree. It stopped there. The work of every stage that had already merged was still on the branch, but nothing recorded *which* stages those were, so the only way forward was to run the whole DAG again and pay for every provider call a second time. For a phone-driven gateway, where the crash cases are a closed laptop and a WSL restart, that made crash recovery a report rather than a recovery.

Two facts shaped where the missing record should live. First, a stage's work becomes real at the moment it merges into the run's branch — before that it is a discardable worktree, after that it is a commit. Second, the queue (`jobs.sqlite`) deliberately knows nothing about git: `core.jobs.store`'s whole design argument is that it stores lifecycle, not repository state.

## Decision

Record per-stage completion in the run's own integration worktree, in its git directory (`.git/worktrees/<name>/`), not in the job database — `core/orchestrator/checkpoint.py`. `ai-platform resume <id>` re-queues the same interrupted job; the worker detects that the job already owns a worktree with a readable checkpoint and hands the supervisor a `Resume`, which adopts that worktree and branch instead of creating new ones.

Four constraints make it safe:

- **Written after a merge, never before.** The checkpoint can only under-claim. A crash between a merge and the write costs one repeated stage; the reverse would drop merged work off the branch and still report success.
- **Stored in the git directory, not the worktree.** `git_ops.commit_all` runs `git add -A`, which would otherwise sweep engine bookkeeping onto the branch under review. The git directory is invisible to `git status` by construction, is per-worktree, and is removed with the worktree a successful run deletes — so nothing can offer to resume a finished run.
- **Base commit, complexity and task set are restored, not re-derived.** The target's HEAD may have moved since the crash, and re-calling the decomposer could select a workflow that contradicts what is already merged.
- **Resuming is never automatic.** `interrupted` becomes the one terminal state that can be reopened, only to `queued`, and only through `resume`. A worker that re-queued crashed jobs by itself would retry, in a loop, exactly the runs most likely to kill the next worker.

Verification and review are deliberately re-run rather than restored: each is one provider call against a tree that has since changed, so redoing them is both cheaper than the DAG and more correct than trusting a stale verdict.

## Consequences

A crash costs at most the stage that was in flight. The job keeps its identity across the interruption, so `status` reads `interrupted -> queued -> running` — one job, one branch, one deliverable — instead of an abandoned job beside a fresh one. Checkpoint and branch cannot drift, because the checkpoint lives inside the thing it describes and dies with it.

The cost is a second durable record with its own version field, and a resume path that must refuse rather than improvise: a missing checkpoint, a mismatched branch or a worktree with something else checked out all raise instead of continuing on the wrong history. A stage that was mid-flight when the worker died leaves a task worktree that resume reports but never deletes — it may hold uncommitted agent work, and `git worktree prune` will not reclaim it precisely because the directory still exists.

Finer-grained resumption — mid-stage, or restoring the review verdict — is not attempted and is not claimed.

## Alternatives

- **A `job_stages` table in `jobs.sqlite`:** rejected. It puts repository state in the queue, contradicting [ADR-005](ADR-005-separate-telemetry-and-job-stores.md), and creates two records that can disagree — the one that disagrees is the one that decides whether real merged work is abandoned.
- **Deriving completed stages from the branch's commit log:** attractive because it adds no state and git is the source of truth, but the stage summaries and file lists that downstream agents are given (`scheduler.build_stage_description`) would have to be parsed back out of commit messages. Structured data recovered by regex from model-written prose is not a foundation for deciding what to re-run.
- **A checkpoint file inside the integration worktree:** rejected — `git add -A` commits it onto the branch under review.
- **Automatic retry of interrupted jobs:** rejected. Without a distinction between "the machine restarted" and "this run kills workers", it is a crash loop that spends quota.
- **Re-running the whole DAG on resume:** the status quo, and the thing being fixed. Correct, but it pays for completed work twice and re-merges commits that are already on the branch.
