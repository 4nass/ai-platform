# Budget policy

The hard admission budget is enforced by `core.jobs.budget` before every provider call and reconciled after it returns. A reservation is held in `jobs.sqlite`, counts against concurrent admissions, and is settled with provider usage (or released only when dispatch never happened).

## Dimensions

Each budget class can declare `max_run_tokens`, `max_stage_tokens`, `max_run_calls`, `max_window_tokens`, `max_run_seconds`, `max_stage_seconds`, `max_run_cost_usd`, `max_stage_cost_usd`, `max_window_cost_usd` and `window_hours`. Zero means unlimited.

## Modes

`soft` admits an over-limit call and reports the overrun. `strict` refuses before dispatch and moves a job to `waiting_approval`. `local_fallback` has the same fail-closed behavior until a local provider is available.

## Time: a reservation is not a deadline

Two different numbers, and conflating them made a generous ceiling behave like a harsh one.

`duration_estimate` decides how much wall-clock to *hold* while a call runs, so two concurrent stages cannot each assume the whole remaining budget is theirs. `remaining_seconds` is what actually *bounds* the call: the tightest declared ceiling minus everything already held or settled. That figure — not the reservation — is passed to the adapter, which still applies its own `TIMEOUT_SECONDS` as an upper bound.

So `max_run_seconds: 3600` leaves a CLI call at its usual 900-second ceiling, while `max_stage_seconds: 120` really does stop it at 120.

## Currency: enforced on what settled, not on what was guessed

No provider here prices a request from its prompt, so there is no pre-call cost estimate to admit against — demanding one refused every call. A USD ceiling is therefore checked against **settled** cost: `admit` refuses the next call once the accumulated real spend would cross the ceiling, and `validate` re-checks after settlement. The residual is inherent: a ceiling can be crossed by the call that crosses it, and is enforced from the following one.

Whether a price is even available is a property of the provider, declared as `REPORTS_COST` in each adapter next to `READS_FILES`:

| Provider | `REPORTS_COST` | Why |
|---|---|---|
| `claude_code` | yes | `--output-format json` carries `total_cost_usd` |
| `codex_cli` | no | reports tokens, never a price for the call |
| `anthropic_api` | no | priced per token upstream; no per-call figure returned |

That declaration is what makes fail-closed mean something. A call from a provider that **cannot** price itself is not a gap — counting it as one would make a currency ceiling refuse every run that touched `codex_cli`. A call from a provider that **should** have priced itself and came back without a figure is a real anomaly, and `strict`/`local_fallback` refuse the next call rather than let the ceiling count against a total known to be short. The judgement is stored per reservation (`cost_expected`), because which provider serves a stage can change between runs and a settled row has to stay readable as the thing it was.

A currency ceiling declared on a project whose providers cannot price their calls therefore constrains nothing. `report()` surfaces this as an unknown cost rather than as a total.

## Enforcement and observability

`Report` exposes reserved, consumed, remaining and unknown values for tokens, seconds and USD; telemetry records `duration_ms`, `provider_duration_ms` and `cost_usd`.

The SQLite schema migrates additively on first use, so existing token-only databases keep their reservations.
