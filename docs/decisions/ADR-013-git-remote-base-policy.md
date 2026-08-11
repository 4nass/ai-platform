# ADR-013: Pin a remote base before creating a run

- Status: Accepted
- Date: 2026-08-11
- Issue: [#33](https://github.com/4nass/ai-platform/issues/33)

## Context

A remote or queued run must not inherit whichever branch happens to be checked out, and it must not silently change its base while providers are working. Fetching a remote can also encounter a behind checkout, divergence, force-pushes, missing upstreams, or unavailable credentials. Delivery is consequential and must not be an accidental side effect of running a task.

## Decision

At admission, the engine creates an immutable `BaseSnapshot` containing the selected ref and SHA, configured remote and base branch, remote-tracking ref/SHA, fetch timestamp, policy and outcome. The integration worktree is created from that exact ref.

Projects choose one explicit policy in `config/projects.yaml`:

- `offline` (default): use the local base without contacting the network.
- `fetch`: fetch only the remote-tracking base ref; accept a remote-ahead checkout but reject divergence.
- `require_up_to_date`: fetch and reject any local/remote drift.

Fetch never checks out, resets or merges the target repository. Resume restores the recorded snapshot rather than re-admitting against a newer `HEAD` or remote. Before delivery, the recorded remote base is revalidated. A push helper requires an explicit approval and never force-pushes; the external-action executor and PR transport remain separate follow-up work (#30/#46/#47).

## Consequences

Runs are reproducible and their telemetry explains exactly which base was used. A remote failure leaves the local checkout and any existing delivery branch intact. The default remains offline for local-first use, while unattended projects can opt into stricter synchronization.

The engine currently provides the synchronization and approval-only push primitives, not an end-to-end authenticated PR operation. That boundary prevents adding network side effects to the core run path before the remote security contract exists.

## Alternatives

- **Always use the checked-out `HEAD`:** rejected because it is implicit and can differ from the intended remote base.
- **Fetch and checkout/reset the target:** rejected because admission would mutate user state and dirty work.
- **Always require a clean, up-to-date checkout:** rejected as too rigid for offline/local work; the policy makes the trade-off explicit.
- **Push automatically after a successful run:** rejected; pushing is a privileged external action and requires a separate approval/executor contract.
