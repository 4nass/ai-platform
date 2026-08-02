# ADR-011: Admission, authorization and approval as one layered boundary

- Status: Accepted
- Date: 2026-08-02

## Context

Four P0 issues ([#25](https://github.com/4nass/ai-platform/issues/25), [#26](https://github.com/4nass/ai-platform/issues/26), [#27](https://github.com/4nass/ai-platform/issues/27), [#28](https://github.com/4nass/ai-platform/issues/28)) describe what has to exist before a request that arrived as chat text may reach this engine. They are separate issues but one boundary: each answers a different question about the same incoming request, and any of them missing makes the others decorative. An allowlist without identity cannot say *whose* project; identity without idempotency spends twice on a retry; budgets without either bound the wrong principal's spending; approvals without budgets have nothing to pause.

The engine already had the opposite property in every layer: `--repo` accepted any path, submission trusted whatever the caller said, quota steered rather than stopped, and a run implicitly authorized everything it could reach.

## Decision

One admission path, four checks, each failing before the next becomes expensive.

**1. What may be reached** — `core/orchestrator/registry.py`. A non-local caller names a project id; the engine resolves it. Paths are canonicalized and contained under declared roots, the repository's identity is verified, and `inspect`/`modify`/`test`/`open_pr` are separate grants. See [ADR-010](ADR-010-project-registry-as-the-admission-boundary.md).

**2. Who is asking, and whether we have answered already** — `core/jobs/envelope.py`. A `Principal` is established by whatever authenticated the connection and travels beside the prompt, never parsed from it. Idempotency is keyed on the transport's own identifiers and enforced by a unique index, so a redelivery returns the original job and a reused key with different content is refused and audited.

**3. What it may spend** — `core/jobs/budget.py`. Capacity is reserved before dispatch and reconciled after, so concurrent runs cannot each admit a call the budget affords once. The gate sits at `scheduler.run_task`, the only place in the engine where a provider adapter is dispatched.

**4. What still needs a person** — `core/jobs/approvals.py`. Actions are automatic, denied, or approval-required per project policy. An approval is bound by fingerprint to the exact inputs displayed, is single-use, expires, and is decided only by the principal who asked.

Three properties hold across all four, and each was chosen against a specific failure:

- **Refusals leak nothing.** An unknown project does not name the ones that exist; an idempotency conflict does not quote the stored request. Probing is the first thing an unauthorized caller does.
- **Checks run again at the moment of use, not only at the moment of request.** A queued job re-resolves its project at claim time; an approval is re-checked against current inputs when consumed. A queue exists so work executes later, which makes a check performed only at submission a snapshot taken at the least useful moment.
- **Nothing is granted implicitly by being possible.** A project declaring no actions gets `inspect` alone; an undeclared budget is unlimited *only* because the interactive path must be ungated; `open_pr` is declarable before it is implemented so it can be withheld rather than granted retroactively.

## Consequences

An unattended request is bounded in what it can reach, what it can spend, and what it can do without a person — and each bound is enforced in one place rather than at every call site. `waiting_approval`, which existed in the job state machine from [#24](https://github.com/4nass/ai-platform/issues/24) with nothing to put in it, is now the state a budget pause lands in with an approval attached.

The cost is four new modules and two new stores' worth of schema in `jobs.sqlite`, plus a second engine config file. Each carries a real ongoing obligation: a migration path, and the discipline that a refusal must commit its audit record explicitly, since `connect()` commits only on a clean exit and the exception reporting a refusal would otherwise roll back the evidence for it. That bug was written twice during this work and caught twice by tests.

What is *not* delivered, and is stated rather than implied: no authenticated transport exists, so the only principal today is the local OS user and gate 2 of [docs/security.md](../security.md) remains partial ([#30](https://github.com/4nass/ai-platform/issues/30)). Budgets bound tokens and calls, not elapsed time or currency. `local_fallback` waits rather than falling back, because there is no local adapter ([#37](https://github.com/4nass/ai-platform/issues/37)). None of this makes the engine safe to expose; it makes the remaining gap nameable.

## Alternatives

- **One "gateway policy" module covering all four:** rejected. They have different lifecycles and different stores — an allowlist is inventory, budgets are tuning, approvals are transient state — and collapsing them would make every policy edit touch the file that decides what is reachable.
- **Enforcing budgets in each adapter:** rejected. "No adapter can bypass the gate" is only checkable if there is one gate; per-adapter enforcement is a convention that holds until the next adapter.
- **Approving an *action type* rather than an instance:** simpler, and wrong. It makes approval a bearer token for a class of action, so the thing eventually done need not be the thing that was read.
- **Auto-approving for the local principal:** rejected as a global mode. Local interactive use is a different *channel* — someone present deciding synchronously — not a weaker policy, and a flag that lowers the bar globally would lower it for the gateway too.
