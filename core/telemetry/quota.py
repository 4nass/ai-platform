"""Subscription budgets, and how close recorded usage is to them.

The engine drives two flat-rate subscriptions. A per-call price is therefore
not a decision variable — it measures something the subscriber cannot act on,
because the money is already spent either way. What binds is quota, and quota
is not something either CLI will tell us: `codex exec --json` emits only
thread/turn/item events, and `claude -p --output-format json` reports a price
with no remaining balance.

So the budget is declared (config/quota.yaml) and the consumption is derived
from telemetry. That split is the honest one — the limits are facts only the
subscriber knows, and inventing them would be worse than asking.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from core.telemetry import store

CONFIG_PATH = Path("config/quota.yaml")
DEFAULT_WINDOW_HOURS = 5.0


@dataclass(frozen=True)
class Budget:
    window_hours: float
    tokens: int


def load_budgets(engine_root: Path) -> dict[str, Budget]:
    """Declared budgets per provider. Missing file or missing provider is not
    an error: pressure is then reported as consumption without a percentage,
    which is still useful and beats refusing to run over a config gap."""
    path = engine_root / CONFIG_PATH
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    providers = data.get("providers") or {}

    budgets: dict[str, Budget] = {}
    for name, raw in providers.items():
        if not isinstance(raw, dict):
            continue
        tokens = raw.get("tokens")
        if not isinstance(tokens, int) or tokens <= 0:
            continue
        window = raw.get("window_hours", DEFAULT_WINDOW_HOURS)
        budgets[name] = Budget(window_hours=float(window), tokens=tokens)
    return budgets


def pressure(engine_root: Path, *, window_hours: float | None = None) -> list[dict]:
    """Per-provider consumption in the window, with its share of budget.

    Both the budgets and the telemetry read here are engine-scoped (shared
    across every target repo the engine operates on) because quota is a
    subscription resource, not a per-project one.

    `used_ratio` is None where no budget is declared — distinct from 0.0,
    which would claim the provider is idle.
    """
    budgets = load_budgets(engine_root)
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
