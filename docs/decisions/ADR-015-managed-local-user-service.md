# ADR-015: Managed local user service is portable and optional

- Status: Accepted
- Date: 2026-08-15
- Issue: #40

## Decision

Ship a managed local user service around the durable queue. The worker contract
is shared across Linux, WSL2 and macOS; supervision is an OS adapter:

- Linux native: systemd user unit;
- WSL2: systemd user unit after enabling WSL systemd;
- macOS: launchd user agents.

The profiles provide local liveness/readiness, bounded restart backoff,
graceful stop, rotating logs and WAL-aware SQLite backup/restore. They run with
the logged-in user's least privilege and load only explicit environment values.

The service is not a gateway and does not expose a listener. REST/SSE and
OpenClaw remain separate adapters with their own authentication boundary.

## Rationale

A foreground worker is unreliable after a terminal disconnect or host restart.
A user service gives predictable lifecycle semantics without a daemon privilege,
network attack surface or provider credential broker. Keeping the worker
contract independent from systemd/launchd makes the local runtime portable.

SQLite backup uses the online backup API rather than copying a live WAL file.
Readiness fails closed for missing paths, corrupt SQLite, unavailable worker
code, or no authenticated provider; optional provider degradation is WARN.

## Consequences

Operators must configure the supervisor and credential paths on each host.
WSL2 requires systemd enablement; macOS uses launchd and has no mount-ordering
directive, so readiness/backoff handles unavailable paths. Backups must be
copied off-disk for disaster recovery. A future multi-machine deployment needs
a distributed lock and a different service adapter.
