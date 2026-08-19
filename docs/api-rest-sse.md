# REST/SSE remote API (#47)

The transport exposes a small authenticated WSGI application through
core.transport.http.create_app. It is transport-only: admission, jobs,
approvals and event cursors remain in their existing modules.

## Authentication

Every request carries X-API-Key, X-Timestamp, X-Nonce and X-Signature. The
signature is the HMAC-SHA256 canonical request from core.transport.auth
(method, exact path including its raw query string, timestamp, nonce and SHA-256
body). TLS is still required.
POST /v1/jobs additionally carries an envelope with channel, sender, chat and
message identifiers; these are checked against the credential principal and
provide durable idempotency.

Credentials for the development server are supplied through the
AI_PLATFORM_TRANSPORT_CREDENTIALS environment variable as a JSON list. Do not
put secrets in YAML, Git, query strings or logs. The built-in local development
server uses a request thread per connection, so a long-lived SSE subscription
does not block job status, cancellation or approval calls. It is not a
production WSGI deployment: production needs a managed WSGI/ASGI host, TLS, a
secret manager, process supervision and reverse-proxy rate/body limits.

`ai-platform serve --host 127.0.0.1 --port 8787` starts the built-in local development server. It emits an access record for every request, including route template, HTTP status, outcome and direct client address. It never logs request bodies, query strings, credential identifiers, signatures, nonces, delivery identifiers or arbitrary paths. A production WSGI host must configure the same `ai_platform.transport.access` logger itself and keep its proxy access logs subject to the same redaction and retention policy.

## Endpoints

JSON request bodies must include `Content-Type: application/json`; unsupported media types are rejected before JSON parsing.

- POST /v1/jobs: JSON {project_id, request, envelope, dirty_policy?}. Project ids
  are resolved through the registry; paths and shell commands are rejected.
- GET /v1/jobs/{id}: compact status for the authenticated principal.
- GET /v1/jobs/{id}/events?cursor=N: replayable SSE. id is the durable cursor;
  Last-Event-ID is accepted for reconnects.
- POST /v1/jobs/{id}/cancel: idempotent cooperative cancellation.
- POST /v1/jobs/{id}/approval: {approval_id, approved, note?}; decisions are
  principal-bound and audited.
- GET /v1/jobs/{id}/artifacts: compact branch/diff/log/preview references.

Unauthorized resources return the same 404 shape as missing resources, so the
API does not disclose job ownership. Error responses are
{"error":{"code":"...","message":"..."}}.
