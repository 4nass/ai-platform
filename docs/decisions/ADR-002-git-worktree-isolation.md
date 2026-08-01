# ADR-002: Git worktrees and delivery branches

- Status: Accepted
- Date: 2026-08-01

## Context

Agent stages need to modify real repositories, sometimes in parallel. Moving the user's checkout, sharing one mutable tree, or relying on model discipline risks corrupting local work and leaking failed-stage edits.

## Decision

Create one integration branch/worktree per run and one isolated branch/worktree per writable stage. Verify path contracts, commit successful stage changes, and merge them into integration. Run target validation in a disposable checkout. Retain `engine/<slug>` as the delivery artifact and never merge or push it automatically.

## Consequences

The user's current branch and uncommitted work remain untouched. Failed stages and merge conflicts are inspectable. Git history preserves stage provenance.

The design creates cleanup, locking, base-revision, hooks, and disk-usage responsibilities. Same-machine mutation is serialized; cross-machine coordination is not provided.

## Alternatives

- **Checkout the run branch in the target tree:** rejected because it changes user state.
- **One shared worktree for all agents:** rejected because parallel and failed work can leak.
- **Patch files as the primary artifact:** rejected because branches preserve ancestry, commits, review tooling, and future CI integration.
