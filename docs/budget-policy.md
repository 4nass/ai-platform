# Budget policy

The hard admission budget is enforced by core.jobs.budget before every provider call and reconciled after it returns. A reservation is held in jobs.sqlite, counts against concurrent admissions, and is settled with provider usage (or released only when dispatch never happened).

## Dimensions

Each budget class can declare max_run_tokens, max_stage_tokens, max_run_calls, max_window_tokens, max_run_seconds, max_stage_seconds, max_run_cost_usd, max_stage_cost_usd, max_window_cost_usd and window_hours. Zero means unlimited.

## Modes

soft admits an over-limit call and reports the overrun. strict refuses before dispatch and moves a job to waiting_approval. local_fallback has the same fail-closed behavior until a local provider is available. Currency is fail-closed in strict/local_fallback when a provider does not return a cost estimate; soft records unknown and continues.

## Enforcement and observability

Time is reserved before dispatch and passed as a timeout to CLI/API adapters. Settlement validates actual duration and reported cost again. Report exposes reserved, consumed, remaining and unknown values for tokens, seconds and USD; telemetry records duration_ms, provider_duration_ms and cost_usd.

The SQLite schema migrates additively on first use, so existing token-only databases keep their reservations.
