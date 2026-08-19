# ADR-016: Channel-neutral notification outbox

- Status: Accepted
- Date: 2026-08-15
- Issue: #42

## Decision

The engine owns notification policy, compact rendering, redaction, preferences
and a durable idempotent outbox. It does not own Signal, WhatsApp, Telegram or
browser SDKs. Gateways inject a sink for the selected channel.

Only approval required, failure, preview ready and completion notify by default.
Every other lifecycle event remains available through replayable job events.
Delivery failures are retried and recorded without changing the engineering
run outcome.

## Rationale

Channel SDKs have different limits, authentication and retry semantics. Keeping
them outside the engine avoids provider coupling and preserves the same
mobile-safe result contract for OpenClaw, a browser or another adapter.
Persisting the outbox makes gateway retries safe across process restarts.

## Consequences

The gateway must provide credentials, rate limiting and concrete network
delivery. The engine can test rendering and delivery semantics without network
access. Large details require an authenticated artifact/event link; they are
never dumped into a phone message.
