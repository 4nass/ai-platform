# ADR-017: Remote exposure readiness, and what a check is allowed to claim

- Status: Accepted
- Tracking: [#49](https://github.com/4nass/ai-platform/issues/49)

## Context

Before this engine accepts a request from anywhere but the local CLI, a set of
controls has to hold: authenticated identity, an allowlisted project registry,
hard budgets, audited privileged actions, a fail-closed sandbox, secrets
retention, TLS, rate limiting. `ai-platform security-check` reports on them and
answers one question — may exposure be enabled?

The first version answered it badly in two specific ways, and both were the
same mistake in different clothes: **a check reported a status stronger than the
evidence it had**.

*TLS and rate limiting* were read from `AI_PLATFORM_TLS_TERMINATED` and
`AI_PLATFORM_RATE_LIMIT` — environment variables an operator sets. Reporting
PASS on those is reporting a claim as an observation. Worse, both were only
evaluated when `AI_PLATFORM_REMOTE_ENABLED` was already true, while the
remediation text told operators to enable exposure only after a GO. The gate
therefore issued its GO in precisely the state where the two controls it exists
to guarantee had never been looked at.

*Audited actions* were confirmed with `callable(executor.ActionExecutor)`. A
class is importable whether or not anything constructs it, whether or not its
schema initialises, and whether or not a handler exists for the actions a
project permits — all three of which were false at the time the check reported
PASS.

There was also an override: a gitignored `config/security-risk-acceptance.json`
that flipped `operator_go` to true while `remote_ready` stayed false, so which
field a caller read decided what it was told.

## Decision

**Preflight is decoupled from exposure.** Every control is evaluated regardless
of `AI_PLATFORM_REMOTE_ENABLED`. The switch is reported as the state it is in,
non-blocking, and a GO means "the prerequisites hold, exposure may be enabled",
never "exposure is safe right now".

**`ATTESTED` is a first-class status, and PASS is reserved for observation.**
TLS terminates upstream of this process; rate limiting lives with it. Neither is
visible from here and no arrangement of environment variables makes it so —
splitting one unverifiable boolean into four would have produced four. Those
controls resolve from a recorded operator attestation: a person states what they
verified, with their name, and it expires. The report says who said it and when,
because the difference between "the engine confirmed this" and "someone said so
on 3 March" is what a reader of a security report needs.

**Attestations are bound to a deployment fingerprint** over the bind host and
port, the TLS endpoint identity and the rate-limit policy identity — exactly the
parameters an attestation speaks about. Move the bind and the statement no
longer describes what runs. Deliberately no wider: a fingerprint that changed on
every unrelated edit would be re-attested by reflex, which is not attesting.

**There is no override.** `RISK_ACCEPTED` is deleted rather than demoted. A
time-bounded exception that authorises nothing is a comment, and one that keeps
the name of a bypass is an invitation. An accepted risk is an attestation with a
short expiry — the same act, an honest name, and an audit row.

**Audited actions are checked as policy and as mechanism.** Policy, read-only
against the real registry: every external action a project permits must have a
registered handler, exposed as `executor.default_handlers()` so the question can
be asked without constructing an executor and creating its tables. No external
action enabled is WARN, not PASS — nothing is being guaranteed. An action
enabled with no handler is FAIL. Mechanism, in a throwaway root with an
explicitly-passed null handler: request, approval, single-use consumption, audit
trail, and refusal of an approval against a changed plan. The health check
cannot reach a real handler by the shape of the object graph, not by convention.

**The server re-runs the gate before any non-loopback bind** and refuses on any
blocking check. A report describes the configuration of the moment it was
produced; between then and the bind an attestation can expire or an address can
move. The pre-existing `AI_PLATFORM_REMOTE_ENABLED` guard is kept in front of it
as an independent second line — a bug in the readiness logic must not be
sufficient on its own to open a socket. `ai-platform serve` is the minimal
operational entry point, so the enforcement is reachable rather than notional.

## Consequences

GO becomes harder, and may be unreachable until a deployment adapter exists to
attest against. That is the intended outcome: the previous gate was reachable
because it accepted weaker evidence.

Every GO and NO_GO is recorded with the fingerprint it was issued against, so
one obtained on a loopback profile cannot silently cover a public one.

**What this does not defend against.** The engine runs as one user on one
machine. Whoever can record an attestation can also open `jobs.sqlite` and write
one directly; the audit trail is not tamper-proof and is not described as such.
What it buys is narrower and real: accidental drift is caught, statements are
attributed and dated, expiry is enforced without anyone remembering, and a
decision is tied to a configuration.

## Alternatives

**Probe TLS from inside the process.** The engine sits behind the terminator; a
successful local connection proves nothing about what the outside world reaches.

**Keep the environment variables as a weaker signal.** A variable an operator
sets that no longer grants anything is a trap — set once, silently inert. They
are removed, and attestation is the only path.

**Leave TLS and rate limiting as non-blocking WARNs.** Honest about the evidence
but silent about the decision: a GO would then be issued with no record that
anyone had ever looked. The attestation carries the same honesty and leaves a
row behind.
