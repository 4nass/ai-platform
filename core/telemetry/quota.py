"""Subscription budgets, and how close recorded usage is to them.

The engine drives two flat-rate subscriptions. A per-call price is therefore
not a decision variable — it measures something the subscriber cannot act on,
because the money is already spent either way. What binds is quota, and quota
is not something either CLI will tell us: `codex exec --json` emits only
thread/turn/item events, and `claude -p --output-format json` reports a price
with no remaining balance.

Declared where used to live in config/quota.yaml, now in config/platform.yaml
(core.orchestrator.platform_config) — the consumption is still derived from
telemetry here. That split is the honest one — the limits are facts only the
subscriber knows, and inventing them would be worse than asking.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.telemetry import store

DEFAULT_WINDOW_HOURS = 5.0


@dataclass(frozen=True)
class Budget:
    window_hours: float
    tokens: int


def pressure(
    engine_root: Path, budgets: dict[str, Budget], *, window_hours: float | None = None
) -> list[dict]:
    """Per-provider consumption in the window, with its share of budget.

    `budgets` comes from the caller's already-loaded `PlatformConfig.quotas`
    rather than being self-loaded here — quota is engine-scoped state read
    once per run, not something worth parsing `platform.yaml` for on every
    call site that wants pressure.

    `used_ratio` is None where no budget is declared — distinct from 0.0,
    which would claim the provider is idle.
    """
    window = window_hours or max(
        (b.window_hours for b in budgets.values()), default=DEFAULT_WINDOW_HOURS
    )

    rows = store.provider_pressure(engine_root, window_hours=window)
    for row in rows:
        budget = budgets.get(row["provider"])
        row["window_hours"] = window
        row["budget_tokens"] = budget.tokens if budget else None
        row["used_ratio"] = (row["total_tokens"] / budget.tokens) if budget else None

    # A provider with a declared budget and no traffic still belongs in the
    # view: "idle" is a routing signal, and its absence reads as missing data.
    seen = {row["provider"] for row in rows}
    for name, budget in budgets.items():
        if name not in seen:
            rows.append(
                {
                    "provider": name,
                    "calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "success_rate": None,
                    "avg_duration_ms": None,
                    "window_hours": window,
                    "budget_tokens": budget.tokens,
                    "used_ratio": 0.0,
                }
            )
    return rows
