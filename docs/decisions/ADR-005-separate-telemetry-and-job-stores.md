# ADR-005: Separate SQLite telemetry and job stores

- Status: Accepted
- Date: 2026-08-01

## Context

Telemetry is mostly append-oriented analytical history. A job queue is mutable coordination state requiring atomic claims, heartbeats, cancellation, recovery, and frequent status updates. Combining them couples retention, migrations, contention, and failure recovery.

## Decision

Use a dedicated `jobs.sqlite` for durable job lifecycle and keep `telemetry.sqlite` for run/provider analytics. Link them by run/job identifiers rather than sharing lifecycle tables. SQLite remains appropriate for one local owner; schemas require explicit migration and transaction tests.

## Consequences

Jobs can be compacted or recovered without rewriting analytical history, and telemetry retention does not corrupt queue semantics. Two databases require coordinated backup, documentation, and correlation.

Delivered in [#24](https://github.com/4nass/ai-platform/issues/24) (`core/jobs/store.py`): `jobs.run_id` is the only link between the two, `jobs.sqlite` gitignored alongside `telemetry.sqlite`.

## Alternatives

- **One SQLite database:** simpler initially, but mixes operational and analytical workloads.
- **External queue/database now:** rejected for local-first complexity; reconsider for multi-machine or multi-user service.
- **In-memory/background process only:** rejected because mobile/disconnected operation requires restart recovery.
