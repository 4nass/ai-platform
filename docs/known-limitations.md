# Known limitations

This page records gaps that affect design or operations. Issue priority remains authoritative in GitHub.

## P0 — blocks safe remote use

| Limitation | Impact | Tracking |
|---|---|---|
| No authenticated transport | Identity and idempotency are enforced engine-side, but nothing yet authenticates a non-local caller, so there is no remote principal to enforce them for | [#26](https://github.com/4nass/ai-platform/issues/26), [#30](https://github.com/4nass/ai-platform/issues/30) |
| Quota is advisory, not a hard budget | Unattended runs cannot guarantee cost/token bounds | [#27](https://github.com/4nass/ai-platform/issues/27) |
| No durable approval gate | Push, deploy, secrets, and destructive actions cannot be safely delegated | [#28](https://github.com/4nass/ai-platform/issues/28) |
| Sandbox can fail open when Bubblewrap is absent | Untrusted target tests can execute with host access | security prerequisite |

## P1 — correctness and operability

| Limitation | Impact | Tracking |
|---|---|---|
| OpenClaw tools/API are not implemented | Gateway has no narrow platform contract | [#30](https://github.com/4nass/ai-platform/issues/30) |
| No structured event stream/cancellation contract | Mobile clients cannot follow or stop a run robustly | [#29](https://github.com/4nass/ai-platform/issues/29) |
| No per-run preview deployment | Browser validation requires manual setup | [#34](https://github.com/4nass/ai-platform/issues/34) |
| No remote base sync/delivery policy | Branches can start from stale local state | [#33](https://github.com/4nass/ai-platform/issues/33) |
| Secrets and data retention are incomplete | Remote operation risks credential and source leakage | [#35](https://github.com/4nass/ai-platform/issues/35) |
| Provider health/quality routing is limited | Failover is deterministic but not deeply adaptive | [#31](https://github.com/4nass/ai-platform/issues/31), [#32](https://github.com/4nass/ai-platform/issues/32) |
| Decomposer only prunes a fixed DAG | Novel workflows cannot be composed dynamically | [#18](https://github.com/4nass/ai-platform/issues/18) |
| Upstream summaries can mislead downstream agents | Dependency hand-off quality can reduce correctness | [#6](https://github.com/4nass/ai-platform/issues/6), [#14](https://github.com/4nass/ai-platform/issues/14) |

## Local engine limitations

- **Dirty trees:** default runs use committed HEAD and intentionally exclude uncommitted changes. A coherent snapshot mode is not fully implemented.
- **Base race:** worktree creation must consistently use the base SHA captured at admission, not a later moving HEAD.
- **Target writes:** context index and graph storage under `.ai-platform/` can write to the original target checkout.
- **Lock scope:** `flock` serializes runs only on the same machine.
- **Git hooks:** repository-wide `core.hooksPath` changes can affect a concurrent user commit. A crashed run's leak no longer survives — `disable_hooks` repairs one on entry, so the synchronous `run` path is covered too and the leak cannot compound — but a per-command hook policy would remove the shared-config window entirely.
- **Crash recovery, resumable but not automatic:** a job worker that dies is reconciled to `interrupted` and `ai-platform resume <id>` continues it, skipping the stages already merged (`core/orchestrator/checkpoint.py`). Resuming is always a deliberate act: nothing re-queues a crashed job by itself, since a worker that did would retry, in a loop, exactly the runs most likely to kill the next worker too. A stage that was mid-flight when the worker died leaves a task worktree that is reported on resume but never deleted — it may hold uncommitted agent work.
- **Interrupted stage granularity:** the checkpoint records merged stages, so a crash costs at most the stage in flight. Finer-grained resumption (mid-stage, or restoring the verification/review verdict) is deliberately not attempted — those are one provider call each against a tree that has since moved.
- **Validation parsing:** malformed `test_command` should fail clearly rather than become an apparent absence.
- **Dry-run accounting:** decomposition may spend provider quota without making that obvious.
- **Model fidelity:** effective model reporting depends on what provider CLIs expose.
- **Context scaling:** indexing is not fully incremental and traversal can be expensive.
- **Provider surface:** OpenAI API is a stub and local models have no adapter.
- **Configuration overlap:** agents, routing, quota, workflow, and target policy are deliberately separate but lack one resolved policy view.

## Documentation rule

When a limitation is fixed, remove or rewrite it here only after tests and public behavior are merged. Update [Feature status](feature-status.md) and add or supersede an ADR if the solution changes an architectural boundary.
