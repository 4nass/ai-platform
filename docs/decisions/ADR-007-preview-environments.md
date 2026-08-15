# ADR-007: Immutable per-run preview environments

- Status: Accepted
- Date: 2026-08-01

## Context

Mobile validation needs a browser URL representing exactly one proposed change. Running a local development server in an agent worktree depends on workstation connectivity, leaks process lifecycle into orchestration, and gives reviewers a mutable target.

## Decision

After tests pass and policy permits deployment, CI/CD should build from the committed delivery revision and deploy a short-lived preview on a unique subdomain. Record the source commit, deployment state, URL, expiry, logs, and teardown result as run artifacts.

Preview deployment is a privileged workflow stage requiring an approval/budget policy, isolated secrets, and cleanup. It must not imply merge approval.

## Consequences

The phone can validate the exact branch artifact through a stable browser URL. Rebuilds are traceable and do not require keeping an agent worktree server alive. The design adds CI provider integration, DNS/TLS, secrets, retention, cost, and orphan cleanup responsibilities.

The provider-neutral lifecycle is delivered in core/previews/manager.py and can be attached to the shared #46 action executor. A concrete CI/provider adapter and production domain configuration remain tracked by [#34](https://github.com/4nass/ai-platform/issues/34).

## Alternatives

- **Expose a local dev server through a tunnel:** faster for experiments but couples availability and security to the workstation.
- **Deploy from an uncommitted worktree:** rejected because the result is not reproducible.
- **Only screenshots:** useful as artifacts but insufficient for interactive validation.
