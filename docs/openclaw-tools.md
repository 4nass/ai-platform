# OpenClaw typed tools

Issue #30 adds an in-process, versioned adapter in core/openclaw.py. It is the
engine-side contract OpenClaw can call; it is not a messaging SDK and it does
not open a socket.

## Tool contract

tool_schemas() returns version v1 and JSON Schema for:

- engineering_submit({project, request, mode});
- engineering_status({run_id});
- engineering_cancel({run_id});
- engineering_approve({run_id, approval_id?, decision, note?});
- engineering_diff({run_id});
- engineering_events({run_id, cursor?, limit?}).

The only submit mode currently supported is modify. The adapter returns a
durable integer run_id immediately and starts a detached worker. Repeating the
same signed envelope returns the existing run without spawning a second
worker.

Status and diff are deliberately compact. They include state, stage, bounded
summary, branch and authenticated references to events, artifacts, logs and
preview. Diff content is paginated by the existing authenticated artifact
view. Events use the durable SQLite cursor and can resume after reconnecting
or an OpenClaw restart.

## Authentication and authority

A network adapter must verify the HMAC request with core.transport.auth before
calling OpenClawTools.call. The call receives an AuthenticatedRequest and the
exact signed body; the adapter rejects a body hash mismatch. The project id
must match the signed envelope and is resolved through config/projects.yaml.
The caller identity comes from the verified Principal, never from a prompt or
tool argument.

Scopes are fixed per tool: submit, read, cancel and approve. Job ownership is
checked by the shared transport service used by both the REST and OpenClaw
adapters, so the two surfaces cannot drift on principal isolation. Jobs,
budgets, provider policy, project actions, worktrees and approval fingerprints
remain owned by AI Platform. OpenClaw cannot provide a filesystem path, shell
command, provider/model choice, budget override or arbitrary URL.

## Reconnect and errors

The durable job and event stores are the source of truth. A fake or real
OpenClaw client can construct a new OpenClawTools instance after a restart and
resume with engineering_status or engineering_events(cursor). Tool errors have
stable codes such as invalid_arguments, project_not_allowed, run_not_found,
approval_required and idempotency_conflict. Messages are compact and do not
include secrets or raw provider output.

Concrete Signal/WhatsApp/Telegram delivery, TLS termination, rate limiting and
credential rotation remain deployment responsibilities of the replaceable
OpenClaw gateway. The adapter is intentionally usable without a network in
unit and end-to-end contract tests.
