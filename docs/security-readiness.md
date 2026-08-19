# Remote security readiness gate

Issue #49 adds a deterministic barrier before exposing the REST/SSE API to a remote network. A working API is not evidence that the engine is safe to expose: each trust boundary has to produce something local and checkable, and each check has to be honest about which kind of evidence it has.

## Commands

```bash
ai-platform security-check
ai-platform security-check --json
```

Exit code 0 for GO, 1 for NO_GO. JSON output is versioned `v1` for CI or release evidence. Secret values are never printed.

The report is a **preflight**: every control is evaluated whether or not `AI_PLATFORM_REMOTE_ENABLED` is set. Requiring exposure to be live in order to check its protections would mean turning the system on to find out whether turning it on is safe.

## Statuses

| Status | Meaning |
| --- | --- |
| `PASS` | This process observed it. |
| `ATTESTED` | A named person verified it, it is recorded, and it expires. Not observed here. |
| `WARN` | Informational, or a capability that is not applicable to this configuration. |
| `FAIL` | Blocking. |

**GO** requires every blocking check to be `PASS` or `ATTESTED`. **NO_GO** otherwise. There is no third decision and no override — a gate with a bypass is the bypass. A blocking `WARN` does not pass.

## Attestation

TLS terminates upstream of this process and rate limiting lives with it. Neither is visible from here, and an environment variable claiming otherwise is a claim, not a check — so `AI_PLATFORM_TLS_TERMINATED` and `AI_PLATFORM_RATE_LIMIT` no longer exist. Record what you actually verified instead:

```bash
ai-platform attest tls_termination --statement "nginx 1.27 terminates TLS on 443, cert valid to 2026-11, port 80 redirects" --days 30
```

An attestation is bound to a **deployment fingerprint** over the bind host and port, `AI_PLATFORM_TLS_ENDPOINT` and `AI_PLATFORM_RATE_LIMIT_POLICY` — the parameters the statement is about. Change the bind address or the proxy and the attestation stops counting; change an unrelated setting and it does not. Maximum life is 90 days.

The report distinguishes never attested, expired and withdrawn, because those have different fixes.

### What this does and does not defend against

The engine runs as one user on one machine. Whoever can record an attestation can also open `jobs.sqlite` and write one directly. **This is not tamper-proof.** What it buys is narrower and real: accidental drift is caught, every statement is attributed and dated, expiry is enforced without anyone remembering to, and every GO/NO_GO is recorded against the fingerprint it was issued for — so a decision reached on a loopback profile cannot silently cover a public one.

## Audited actions

Two checks, because "can the engine perform an audited action" and "does it in fact refuse an unapproved one" are different questions.

**Policy** — read-only, against the real registry. Every external action a project permits (`open_pr`, `git_push`, `preview_deploy`) must have a handler in `executor.default_handlers()`. No external action enabled is `WARN`: nothing is being guaranteed. An action enabled with no handler is `FAIL` — a promise the engine cannot keep.

**Mechanism** — in a throwaway engine root with an explicitly-passed null handler: a privileged action stops for approval, the approval is consumed once, the execution is audited, and an approval granted against one plan is refused for a changed one. The check cannot reach a real handler by the shape of the object graph, and writes nothing to the live queue.

## Serving

```bash
ai-platform serve --host 127.0.0.1 --port 8787
```

A loopback bind serves directly. A **non-loopback bind re-runs the whole gate first** and refuses on any blocking check; there is no flag to skip it. The report produced before deployment describes the configuration of that moment, and this is the moment that matters. The `AI_PLATFORM_REMOTE_ENABLED` switch is checked first and independently — a bug in the readiness logic must not be enough on its own to open a socket.

The server is minimal and belongs behind a TLS-terminating reverse proxy with rate limiting.

## Evidence matrix

| Boundary | Evidence | Current state |
| --- | --- | --- |
| Identity and replay | HMAC principal, scopes, nonce ledger and idempotency | Engine delivered (#44) |
| Project admission | Registry id, canonical path and allowed actions | Delivered (#25) |
| API contract | Authenticated REST/SSE, status, events, cancel, approvals and artifacts | Engine delivered (#47) |
| Lifecycle | Durable events, cursors and cooperative cancellation | Engine delivered (#29) |
| OpenClaw | Typed submit/status/cancel/approve/diff/events adapter | Engine delivered (#30) |
| Git delivery | Base synchronization, divergence policy and approval-bound push | Engine delivered (#33/#46) |
| Preview | Immutable plan, capability URL, TTL and cleanup lifecycle | Engine delivered; concrete provider remains (#34) |
| Budgets | Token/call reservations, time ceilings and settled-cost currency ceilings | Delivered (#27/#45) |
| Secrets | Redaction and retention policy | Redaction primitives exist; complete policy/evidence remains (#35) |
| Sandbox | Bubblewrap and committed target policy | Host-dependent; required for remote readiness |
| Service/notifications | Managed local service and durable notification outbox | Engine delivered (#40/#42) |
| TLS and rate limiting | Operator attestation bound to the deployment fingerprint | Attestable; no deployment adapter can prove it yet (#47) |

## Rollback

Set `AI_PLATFORM_REMOTE_ENABLED=false` and restart the managed local user service. Credentials come from the service secret manager, never from Git-tracked YAML.

## MVP status

The repository remains NO_GO until #35 retention evidence, host sandbox prerequisites, production credentials, and TLS/rate-limit attestations against a real deployment are in place. GO is deliberately harder to reach than it was: the previous gate was reachable because it accepted weaker evidence.
