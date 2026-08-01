# ADR-008: Two-tier configuration — platform.yaml plus internal presets

- Status: Accepted
- Date: 2026-08-01

## Context

Six engine-level files (`config/agents.yaml`, `routing.yaml`, `quota.yaml`, `context.yaml`, `workflow.yaml`, `token_budget.yaml`) had to be read together to predict what a run would do. Each was real, calibrated policy — per-role/per-complexity provider profiles, measured routing gates, relevance floors tuned against real requests — but exposing all of it as user-editable config was more than a phone-driven personal gateway should require someone to understand before saying "fix this, test it, show me a preview." `docs/configuration.md` tracked this as issue [#41](https://github.com/4nass/ai-platform/issues/41).

## Decision

Two tiers. `config/platform.yaml` is the only engine file meant for hand-editing: `profile`, `providers.quotas`, `routing.*`, `workflow.mode`/`max_parallel`/`decompose`/`max_correction_attempts`, `context.mode`, and an optional `advanced.context` escape hatch. Everything else — per-role provider/model/effort tables, the DAG shape, retrieval flags and numeric floors — is a named preset shipped inside the engine under `config/presets/{profiles,workflow,context}/<name>.yaml`, resolved by name from `platform.yaml` rather than read from a fixed path.

`core/orchestrator/platform_config.py` mirrors `target_config.py`'s pattern: one frozen `PlatformConfig` dataclass, a pure `load()` entry point, preset names validated against what is actually on disk (not a hardcoded Python allowlist, so adding a preset is a data change, not a code change). Loaded once per run in `supervisor.run()` and threaded through as an optional parameter everywhere a module used to self-load its own file (`router.route`, `planner.plan`, `ContextManager.__init__`, `scheduler.run_task`) — one config snapshot per run, so an unknown-preset typo surfaces once, early, rather than five calls deep into whichever module read it first, and a run stays internally consistent even if `platform.yaml` changes mid-run. Standalone callers (the `route`/`context`/`quota`/`config` CLI commands, most tests) keep working via a self-load default.

Two profile presets shipped: `balanced` (the previous `agents.yaml`, renamed verbatim) and `max` (each role's already-declared "critical" tier promoted to the unconditional base profile — a mechanical reshuffle of existing numbers, not new tuning). `cheap` and `local` were deliberately not invented: this codebase has no per-token cost model to calibrate "cheap" against (flat subscriptions; routing already demotes dollars, see `router.py`), and no local-model adapter exists yet (issue [#37](https://github.com/4nass/ai-platform/issues/37)). Three context presets shipped (`smart`/`full`/`minimal`), varying only the retrieval toggles and injection mode — the calibrated numeric floors (`min_similarity`, `min_lift`) are identical across all three, since recalibrating them without new measurement would be the same mistake in a different file.

`config/token_budget.yaml` was deleted outright rather than folded into a preset: its only reader, `providers/anthropic_api/adapter.py`, is never selected by a default profile, so its per-role output-token cap is adapter-internal plumbing (`TOKEN_BUDGETS` module constant), not part of the platform's calibrated policy surface.

`.ai-platform.yml` is untouched — target-repo policy, a different axis entirely (see ADR-003).

## Consequences

Daily use requires touching one small file with five real knobs; the calibrated tables move with the engine's own version history instead of being edited in place. A user who wants a genuinely different policy authors a new preset file rather than hand-editing `platform.yaml` into `agents.yaml`'s old shape. Threading `PlatformConfig` as an object rather than exploding it into scalars at every call site kept the signature growth to one parameter per function, including through the `ThreadPoolExecutor.submit(...)` DAG-dispatch path — the single spot in the migration that needed line-by-line attention rather than mechanical find-replace, since a positional-argument reorder there fails silently rather than loudly.

A real benefit surfaced during migration, not just at design time: several tests that previously wrote a fake `config/agents.yaml`/`routing.yaml`/`quota.yaml` tree to exercise one scenario now construct a `PlatformConfig(...)` directly in memory (the same precedent `router.py`'s in-memory `Thresholds` override already established) — less fixture code for the same coverage.

## Alternatives

- **Keep the six files, just document them better:** rejected — the problem was surface area, not discoverability. A well-documented six-file system is still a six-file system to reason about before a routine change.
- **Flatten everything into `platform.yaml` directly:** rejected — the per-role profile tables and the DAG are real, multi-dimensional, calibrated data; inlining them would make the one file meant to stay small balloon back to the same size as the six it replaced.
- **Ship `cheap`/`local` presets now with plausible-looking values:** rejected — this codebase's own calibration discipline (every existing numeric floor is commented "starting point, revisit against real usage") exists specifically to prevent inventing config that looks authoritative but isn't measured.
