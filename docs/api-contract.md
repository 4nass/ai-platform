# REST/SSE API contract

- Status: Contract baseline
- Version: `v1`
- Tracking: [#47](https://github.com/4nass/ai-platform/issues/47), [#30](https://github.com/4nass/ai-platform/issues/30)

This document defines the stable boundary between user interfaces (OpenClaw, a browser, CLI adapters or future notification clients) and AI Platform. It is a design contract, not a claim that the remote server is already deployed. The transport verifier is delivered as an engine building block; the REST/SSE server and public exposure gate remain tracked by #47 and #49. Until then, the local CLI remains the supported interface.

## Ownership boundary

The adapter owns channel concerns only:

- receive and send messages;
- map a conversation to an authenticated principal;
- render progress, approvals, errors and links;
- retry requests using the same idempotency key.

AI Platform owns all engineering semantics:

- project allowlisting and authorization;
- job state, idempotency and cancellation;
- context, workflow and provider/model/effort routing;
- token/call budgets and approvals;
- Git base synchronization, worktrees, tests, review and artifacts.

An adapter never receives an arbitrary repository path, shell command, provider command, token, secret or internal database handle. It calls typed operations only.

## Transport and versioning

- Base path: `/v1`.
- HTTPS is required outside localhost.
- Requests use a rotating HMAC credential with `X-AI-Platform-Key-Id`, `X-AI-Platform-Timestamp`, `X-AI-Platform-Nonce` and `X-AI-Platform-Signature` headers. HTTPS is still required.
- The signature covers the protocol version, method, path, timestamp, nonce and SHA-256 body hash. The credential identifies a channel-scoped principal and scopes, never a project.
- JSON is UTF-8. Timestamps are ISO-8601 UTC.
- Breaking changes require a new major path (`/v2`). Additive fields and event types are allowed in `v1`; clients must ignore unknown fields and event types.
- Every response includes `request_id` for support and audit correlation.

### Request authentication

The current transport-neutral verifier is implemented in `core/transport/auth.py`; an HTTP adapter must call it before parsing or dispatching an operation. A credential is identified by a non-secret key id and carries a channel, principal id, scopes, activation/expiry window and revocation state. Multiple active credentials may overlap during rotation. Secrets are injected by the service runtime and never stored in repository configuration.

The nonce ledger is durable (`ReplayStore`) and keyed by credential plus nonce. A repeated nonce with the same signed body is an idempotent retry and may proceed to the job store, which returns the original job. Reusing it with different content is rejected. Timestamps have a bounded skew window, and channel/sender/chat/message fields are part of the signed request envelope.

## Operations

### Submit a job

`POST /v1/jobs`

Required headers:

```http
X-AI-Platform-Key-Id: key-2026-01
X-AI-Platform-Timestamp: 1786478400
X-AI-Platform-Nonce: <fresh-random-value>
X-AI-Platform-Signature: <base64url-hmac>
Idempotency-Key: <derived-from-signed-envelope>
Content-Type: application/json
```

`Idempotency-Key` must equal the key derived from the signed transport envelope. The server may recompute it and reject a mismatch.

Request:

```json
{
  "project_id": "ai-platform",
  "request": "Add a health endpoint",
  "operation": "modify",
  "dry_run": false,
  "transport": {
    "channel": "openclaw",
    "sender_id": "owner-1",
    "chat_id": "chat-1",
    "message_id": "message-1",
    "sent_at": "2026-08-11T20:00:00Z"
  }
}
```

`project_id` is an engine-owned registry id. `operation` is one of the allowlisted typed actions (`inspect`, `modify`, `test`, `open_pr`); the server checks authorization and project policy. The caller cannot select a filesystem path, base SHA, provider, model, effort, budget or remote URL. Those values come from the admitted project and platform policy and are recorded on the run.

First response: `202 Accepted`. Repeating the same `Idempotency-Key` with the same request returns the original job representation; reusing it with different inputs returns `409 idempotency_conflict`.

```json
{
  "request_id": "req_01...",
  "job": {
    "id": "job_01...",
    "state": "queued",
    "project_id": "ai-platform",
    "created_at": "2026-08-11T20:00:00Z",
    "links": {
      "self": "/v1/jobs/job_01...",
      "events": "/v1/jobs/job_01.../events"
    }
  }
}
```

### Read status

`GET /v1/jobs/{job_id}`

Returns the durable job state, current stage, recorded base identity, approval requests and links to available artifacts. It never returns provider credentials or raw secrets. A caller sees only jobs authorized for its principal.

Stable state values:

`queued`, `running`, `waiting_approval`, `cancel_requested`, `succeeded`, `failed`, `cancelled`, `interrupted`.

The representation may gain fields, but `id`, `state`, `project_id`, `created_at`, `updated_at` and `links` remain stable in `v1`.

### Stream and replay events

`GET /v1/jobs/{job_id}/events`

Response: `text/event-stream`. Events are ordered by a per-job monotonically increasing `seq`. Clients reconnect with `Last-Event-ID` (the event id is the sequence encoded as a string); the server replays events after that sequence before sending live events. Without a cursor, the server sends a bounded recent history followed by live events.

Example:

```text
id: 17
event: stage.updated
data: {"job_id":"job_01...","seq":17,"occurred_at":"2026-08-11T20:02:00Z","stage":"tests","state":"running"}

```

Reserved event types:

- `job.created`, `job.updated`, `job.completed`;
- `stage.updated`;
- `provider.selected`;
- `budget.updated`;
- `approval.required`, `approval.resolved`;
- `artifact.available`;
- `warning`, `error`;
- `heartbeat` (transport health only, not a job state transition).

Events are facts, not commands. A client must use the operation endpoints for cancellation or approval.

### Cancel a job

`POST /v1/jobs/{job_id}/cancel`

The operation is idempotent. It records `cancel_requested` and cooperatively stops at the next safe boundary; it does not kill an arbitrary process or delete a worktree. A terminal job returns its existing terminal state.

### Resolve an approval

`POST /v1/jobs/{job_id}/approvals/{approval_id}`

Request:

```json
{ "decision": "approve" }
```

`decision` is `approve` or `deny`. The server checks that the approval is scoped to the principal, single-use, unexpired and bound to the exact operation fingerprint. The adapter never approves by sending free-form text.

### List artifacts

`GET /v1/jobs/{job_id}/artifacts`

Returns metadata and short-lived, authorized links for logs, diff, delivery branch, preview or test output. Artifacts are immutable references to a recorded revision; the endpoint does not expose arbitrary filesystem paths.

### Read preview status

GET /v1/jobs/{job_id}/preview

Returns the authenticated preview record when a deployment exists. It includes
the immutable commit_sha, provider status, expiring URL, optional logs URL,
data isolation mode and expires_at. The URL is an artifact, not an
authorization bypass: the provider edge must enforce its provider auth or
capability token. The endpoint is principal-bound like the other job reads.

The SSE stream also carries preview.requested, preview.deploying, preview.ready,
preview.failed, preview.expired, preview.cleaned and preview.cleanup_failed
events. Clients should use the preview status endpoint after reconnecting
rather than infer lifecycle from one event.

## Error envelope

All non-2xx responses use one shape:

```json
{
  "request_id": "req_01...",
  "error": {
    "code": "project_not_allowed",
    "message": "The requested operation is not allowed for this project.",
    "retryable": false,
    "details": {}
  }
}
```

Stable codes include `unauthenticated`, `forbidden`, `invalid_request`, `project_not_found`, `project_not_allowed`, `idempotency_conflict`, `job_not_found`, `approval_required`, `conflict`, `rate_limited`, `budget_exceeded`, `git_diverged`, `provider_unavailable`, `cancelled` and `internal_error`. Messages are safe for the caller; secrets, access tokens and uncontrolled command output are not returned.

## OpenClaw mapping

| User intent | API operation | Adapter responsibility |
|---|---|---|
| Start work | `POST /v1/jobs` | Parse message, choose project id from trusted conversation context, display job id |
| How is it going? | `GET /v1/jobs/{id}` or SSE | Render current state and latest stage |
| Follow live progress | `GET /v1/jobs/{id}/events` | Subscribe, reconnect with `Last-Event-ID`, translate events to messages |
| Stop work | `POST .../cancel` | Confirm user intent, submit idempotent cancel |
| Approve push/deploy | `POST .../approvals/{id}` | Show exact scope, collect explicit decision |
| Review result | `GET .../artifacts` | Render links to branch, logs, tests or preview |

This mapping is intentionally replaceable: a browser or CLI client uses the same operations and receives the same job/event semantics.

## Non-goals

The `v1` contract does not expose arbitrary shell execution, direct provider prompts, raw Git push, database access or webhook secrets. The typed engine adapter is documented in [openclaw-tools.md](openclaw-tools.md); channel formatting and network delivery remain replaceable gateway concerns, while authorization and durable state remain testable in AI Platform.
