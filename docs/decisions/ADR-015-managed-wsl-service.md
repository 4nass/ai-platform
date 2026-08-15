# ADR-015: Managed WSL user service is local and optional

- Status: Accepted
- Date: 2026-08-15
- Issue: #40

## Decision

Ship a user-level systemd service for WSL2 as an operational adapter around the
durable queue. It provides local liveness/readiness checks, bounded restart
backoff, graceful stop, rotating logs, and WAL-aware SQLite backup/restore.
It runs with the Unix user's least privilege and loads only an explicit,
0600 environment file.

The service is not a gateway and does not expose a listener. REST/SSE and
OpenClaw remain separate adapters with their own authentication boundary.
Host-specific WSL systemd enablement is documented but is not hidden in the
engine.

## Rationale

A foreground worker is unreliable after a terminal disconnect or WSL restart.
A user service gives predictable lifecycle semantics without introducing a
daemon privilege, network attack surface, or provider credential broker.
SQLite backup uses the online backup API rather than copying a live WAL file.
Readiness fails closed for missing mounts, corrupt SQLite, unavailable worker
code, or no authenticated provider; optional provider degradation is WARN.

## Consequences

Operators must enable systemd and lingering on each WSL distribution. Backups
must be copied off-disk for disaster recovery. A hard kill can leave a job
interrupted, which startup reconciliation records explicitly. A future
multi-machine deployment needs a distributed lock and a different service
adapter.
