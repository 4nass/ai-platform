# Mobile notifications

Issue #42 delivers a provider-neutral notification outbox. The engine decides
which lifecycle events matter, renders a compact safe message and records
delivery state. OpenClaw or another gateway supplies the Signal, WhatsApp,
Telegram or browser sink; the engine never imports a messaging SDK.

## Event policy

Only meaningful transitions notify by default:

| Event | Severity | Default next action |
|---|---|---|
| approval.required | warning | approve or deny the job |
| run.failed / preview.failed | error | inspect status and events |
| preview.ready | info | open the authenticated preview |
| run.completed | info | review the branch and preview |

Other stage/progress events remain available through the durable event stream
and do not flood a phone.

## Rendering and safety

core.notifications.render_event produces the same structured Notification
contract for every channel. Channel limits are enforced for Signal, WhatsApp,
Telegram and browser views. Summaries, branches and notes are bounded,
control characters removed and credential-like values redacted. Links are
included only when they are HTTPS or an authenticated /v1/ reference. Large
details are represented by the authenticated artifact/event view rather than
sent in full.

The message includes project/run identity, state, safe summary, branch, token
and call usage when telemetry is available, and a channel-neutral next action.

## Preferences

Use configure_preference with project id, channel, recipient, minimum severity
and an optional event allowlist. Preferences are stored in
notifications.sqlite. If no preference exists, the submitted envelope's
channel and chat/sender identity receive only the meaningful default events.
A configured project preference is authoritative, including disabling a
channel.

## Delivery contract

notify_event accepts injected sinks and never lets a sink exception change the
job state. Each delivery is uniquely keyed by event id, channel and recipient.
A repeated event or gateway retry reuses the existing outbox row and cannot
send a delivered notification twice. Failed sends use bounded exponential
retry and retain the last error; the outbox can be polled with pending().

The outbox is included in the managed-service SQLite backup set. Concrete
channel credentials, rate limits and webhook retries belong to the gateway
adapter, not the engine.
