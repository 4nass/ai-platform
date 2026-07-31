# Documentation

This directory documents the implemented system. The root README mixes the current prototype with the long-term vision; the guides below are the operational reference for the code that runs today.

- [Architecture](architecture.md) — execution flow, boundaries, isolation, and failure handling.
- [Model routing policy](model-routing-policy.md) — complexity classes, Claude/Codex profiles, fallback rules, and rationale.
- [Configuration](configuration.md) — YAML schemas, target-repository settings, and compatibility rules.
- [Operations](operations.md) — setup, inspection commands, telemetry, quotas, and troubleshooting.
- [Testing](testing.md) — test layers, safe local validation, and real-provider smoke tests.

## Design principles

1. Keep the engine separate from the repository it modifies.
2. Let configuration choose bounded profiles; do not let an agent invent a model name.
3. Treat the profile order as preference plus fallback, not as a multi-model vote.
4. Make complexity explicit and conservative: unparseable classifications become `complex`.
5. Enforce safety at the process and Git boundaries, not only in prompts.
6. Record requested and effective execution settings so routing decisions remain auditable.
7. Never block all work because every candidate is gated; run the declared first profile and explain why.

## Source of truth

Runtime behavior is defined by:

- `config/agents.yaml` for per-role model profiles;
- `config/routing.yaml` and `config/quota.yaml` for measurable gates;
- `config/workflow.yaml` for the task graph and correction limit;
- `prompts/*.md` for role contracts;
- `core/orchestrator/` and `providers/` for enforcement.

When documentation and code disagree, tests and code win; update this directory in the same change.
