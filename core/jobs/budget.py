"""Hard spending limits, enforced before a call rather than measured after it.

`core.telemetry.quota` is advisory by design: it compares recorded consumption
against a declared allowance and *demotes* a pressured provider, but the router
runs the first profile anyway when every candidate is gated, because refusing
to run is worse than running expensively for someone sitting at a terminal.

That is the wrong trade for a request from a phone. Nobody is watching, the
correction loop can retry, and "expensive" has no ceiling. Issue
[#27](https://github.com/4nass/ai-platform/issues/27) is the other half:
limits that stop a run instead of steering it.

**Reservations, not just accounting.** Checking consumption after each call
cannot bound anything — by the time the number moves, the tokens are spent, and
two concurrent runs each see the other's spending only once it is over. So
capacity is *reserved* before dispatch and reconciled with the real figure
afterwards. Admission sums held reservations *and* settled ones, which is what
makes two jobs running at once unable to each admit a call the budget can only
afford once.

**Estimates are honest about being estimates.** No local tokenizer covers a
subscription CLI, so the pre-call figure is a documented character heuristic
plus a fixed output allowance. It is deliberately biased to over-reserve: a
reservation that is too large delays a call, one that is too small permits a
call the budget could not afford, and only the first is recoverable. The real
number replaces the estimate the moment the provider reports it.

**Held rows are reclaimed, never trusted to be tidy.** A crashed worker leaves
reservations held against a run that is no longer spending. Left alone they
would shrink every later run's budget forever, so reconciliation releases them
on age, the same signal and the same reasoning as a stale heartbeat.

Lives in `jobs.sqlite` rather than `telemetry.sqlite` for the reason ADR-005
gives: this is mutable coordination state between concurrent workers, not
append-only analytical history.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.jobs.store import connect

SOFT = "soft"
"""Record and report, never block. The interactive default, and what the
engine did before this module existed."""

STRICT = "strict"
"""Refuse the call and pause the run for a human decision rather than exceed a
limit. The mode an unattended request should run under."""

LOCAL_FALLBACK = "local_fallback"
"""Move the work to a local model instead of refusing it. Selectable, and
currently equivalent to `strict` in effect: no local adapter exists yet
([#37](https://github.com/4nass/ai-platform/issues/37)), so there is nothing to
fall back *to*. It waits rather than silently spending on a paid provider —
which is the behaviour the criterion asks for when no local profile is
eligible, and the only honest one until one is."""

MODES = (SOFT, STRICT, LOCAL_FALLBACK)

HELD = "held"
SETTLED = "settled"
RELEASED = "released"

CHARS_PER_TOKEN = 3.6
"""Characters per token for prompt estimation.

A heuristic, and labelled as one everywhere it surfaces. Real tokenizers are
model-specific and the two delivered providers are subscription CLIs that
expose none locally, so the choice is between a documented approximation and
no pre-call bound at all. Slightly below the usual 4.0 rule of thumb on
purpose: source code tokenizes denser than prose, and under-estimating is the
failure that admits a call the budget cannot afford."""

DEFAULT_OUTPUT_RESERVE = 12000
"""Tokens set aside for a response nobody can measure in advance. Reconciled
away the moment the provider reports what it actually produced; until then, a
budget that counted only the prompt would be blind to the larger half of many
calls."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS reservations (
  id INTEGER PRIMARY KEY,
  run_key    TEXT NOT NULL,
  stage      TEXT NOT NULL DEFAULT '',
  agent      TEXT NOT NULL DEFAULT '',
  provider   TEXT NOT NULL DEFAULT '',
  estimated  INTEGER NOT NULL,
  actual     INTEGER,
  estimated_seconds REAL NOT NULL DEFAULT 0,
  actual_seconds REAL,
  estimated_cost_usd REAL,
  actual_cost_usd REAL,
  state      TEXT NOT NULL,
  created_at TEXT NOT NULL,
  settled_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_reservations_run   ON reservations(run_key);
CREATE INDEX IF NOT EXISTS idx_reservations_state ON reservations(state);
"""

STALE_AFTER_SECONDS = 3600.0
"""How long a reservation may stay `held` before reconciliation reclaims it.
Far more generous than a job heartbeat: a single provider call on a critical
profile can legitimately run for many minutes, and reclaiming a live
reservation would let the budget be spent twice."""


class BudgetExceeded(Exception):
    """A call that would cross a hard limit, refused before it was made.

    Carries the decision so a caller can report which limit, by how much, and
    under which mode — "budget exceeded" alone leaves the person who has to
    approve or raise the limit with nothing to act on.
    """

    def __init__(self, decision: Decision):
        super().__init__(decision.reason)
        self.decision = decision


@dataclass(frozen=True)
class Limits:
    """What one budget class allows. Zero means unlimited, on every field.

    Unlimited-by-default matters: this module is loaded for every run,
    including the interactive ones nobody wants gated, so an undeclared budget
    has to behave exactly as if the module were not there.
    """

    max_run_tokens: int = 0
    max_stage_tokens: int = 0
    max_run_calls: int = 0
    max_window_tokens: int = 0
    max_run_seconds: float = 0
    max_stage_seconds: float = 0
    max_run_cost_usd: float = 0
    max_stage_cost_usd: float = 0
    max_window_cost_usd: float = 0
    window_hours: float = 24.0

    @property
    def declared(self) -> bool:
        return any(
            (self.max_run_tokens, self.max_stage_tokens, self.max_run_calls, self.max_window_tokens, self.max_run_seconds, self.max_stage_seconds, self.max_run_cost_usd, self.max_stage_cost_usd, self.max_window_cost_usd)
        )


@dataclass(frozen=True)
class Usage:
    """What is already committed, counting money not yet spent.

    `run_tokens` and `window_tokens` include **held** reservations, not only
    settled ones. That is the entire reason reservations exist: two concurrent
    jobs that each looked only at completed calls would both admit a call the
    budget can afford once.
    """

    run_tokens: int = 0
    run_calls: int = 0
    window_tokens: int = 0
    run_seconds: float = 0.0
    stage_seconds: float = 0.0
    window_seconds: float = 0.0
    run_cost_usd: float = 0.0
    stage_cost_usd: float = 0.0
    window_cost_usd: float = 0.0
    cost_unknown: bool = False


@dataclass(frozen=True)
class Decision:
    """Whether a call may proceed, and everything needed to explain it."""

    allowed: bool
    mode: str = SOFT
    reason: str = ""
    limit: str = ""
    estimated: int = 0
    would_total: int = 0
    ceiling: int = 0
    estimated_seconds: float = 0.0
    estimated_cost_usd: float | None = None

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "mode": self.mode,
            "limit": self.limit,
            "estimated_tokens": self.estimated,
            "would_total": self.would_total,
            "ceiling": self.ceiling,
            "reason": self.reason,
            "estimated_seconds": self.estimated_seconds,
            "estimated_cost_usd": self.estimated_cost_usd,
        }


@dataclass(frozen=True)
class Report:
    """Reserved, consumed and remaining for one run — the closing figures.

    `reserved` and `consumed` differ by exactly the estimation error, which is
    the number that says whether `CHARS_PER_TOKEN` is calibrated for the work
    this engine actually does.
    """

    reserved: int = 0
    consumed: int = 0
    calls: int = 0
    limit: int = 0
    mode: str = SOFT
    decisions: list = field(default_factory=list)
    reserved_seconds: float = 0.0
    consumed_seconds: float = 0.0
    reserved_cost_usd: float = 0.0
    consumed_cost_usd: float = 0.0
    cost_unknown: bool = False
    seconds_limit: float = 0.0
    cost_limit_usd: float = 0.0
    stage_cost_limit_usd: float = 0.0
    window_cost_limit_usd: float = 0.0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.consumed) if self.limit else 0

    def line(self) -> str:
        if not self.limit:
            base = f"{self.consumed:,} tokens over {self.calls} calls (no limit declared)"
        else:
            base = (
                f"{self.consumed:,} of {self.limit:,} tokens over {self.calls} calls "
                f"({self.remaining:,} left, {self.reserved:,} reserved, mode {self.mode})"
            )
        parts = []
        if self.seconds_limit:
            parts.append(f"{self.consumed_seconds:.1f}/{self.seconds_limit:.1f}s")
        if self.cost_limit_usd or self.stage_cost_limit_usd or self.window_cost_limit_usd:
            value = "unknown" if self.cost_unknown else f"USD {self.consumed_cost_usd:.4f}"
            ceilings = [v for v in (self.cost_limit_usd, self.stage_cost_limit_usd, self.window_cost_limit_usd) if v]
            parts.append(f"{value}/USD {max(ceilings):.4f}")
        return base + (f"; {'; '.join(parts)}" if parts else "")

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.seconds_limit - self.consumed_seconds) if self.seconds_limit else 0.0

    @property
    def remaining_cost_usd(self) -> float:
        return max(0.0, self.cost_limit_usd - self.consumed_cost_usd) if self.cost_limit_usd else 0.0


def estimate_tokens(*texts: str, output_reserve: int = DEFAULT_OUTPUT_RESERVE) -> int:
    """Pre-call size of a request, in provider tokens. An estimate — see
    `CHARS_PER_TOKEN` for why an exact figure is not available here."""
    characters = sum(len(text or "") for text in texts)
    return int(characters / CHARS_PER_TOKEN) + output_reserve


def _ensure(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    columns = {row[1] for row in con.execute("PRAGMA table_info(reservations)")}
    for name, definition in (("estimated_seconds", "REAL NOT NULL DEFAULT 0"),
                             ("actual_seconds", "REAL"),
                             ("estimated_cost_usd", "REAL"),
                             ("actual_cost_usd", "REAL")):
        if name not in columns:
            con.execute(f"ALTER TABLE reservations ADD COLUMN {name} {definition}")


def _window_start(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def usage(engine_root: Path, limits: Limits, *, run_key: str, stage: str = "") -> Usage:
    """What this run and this window have committed so far.

    `COALESCE(actual, estimated)` is what makes held and settled reservations
    comparable: a call in flight counts at what it was reserved for, and swaps
    to the real figure the moment it settles. Released rows count for nothing —
    that is what releasing means.
    """
    with connect(engine_root) as con:
        _ensure(con)
        run = con.execute(
            "SELECT COALESCE(SUM(COALESCE(actual, estimated)), 0) AS tokens, COUNT(*) AS calls,"
            " COALESCE(SUM(COALESCE(actual_seconds, estimated_seconds)), 0) AS seconds,"
            " COALESCE(SUM(COALESCE(actual_cost_usd, estimated_cost_usd)), 0) AS cost,"
            " SUM(CASE WHEN actual_cost_usd IS NULL AND estimated_cost_usd IS NULL THEN 1 ELSE 0 END) AS unknown"
            " FROM reservations WHERE run_key = ? AND state <> ?",
            (run_key, RELEASED),
        ).fetchone()
        stage_row = con.execute(
            "SELECT COALESCE(SUM(COALESCE(actual_seconds, estimated_seconds)), 0) AS seconds,"
            " COALESCE(SUM(COALESCE(actual_cost_usd, estimated_cost_usd)), 0) AS cost"
            " FROM reservations WHERE run_key = ? AND stage = ? AND state <> ?",
            (run_key, stage or "", RELEASED),
        ).fetchone()
        window = con.execute(
            "SELECT COALESCE(SUM(COALESCE(actual, estimated)), 0) AS tokens,"
            " COALESCE(SUM(COALESCE(actual_seconds, estimated_seconds)), 0) AS seconds,"
            " COALESCE(SUM(COALESCE(actual_cost_usd, estimated_cost_usd)), 0) AS cost"
            " FROM reservations WHERE state <> ? AND created_at >= ?",
            (RELEASED, _window_start(limits.window_hours)),
        ).fetchone()
    return Usage(
        run_tokens=int(run["tokens"]), run_calls=int(run["calls"]),
        window_tokens=int(window["tokens"]),
        run_seconds=float(run["seconds"] or 0), stage_seconds=float(stage_row["seconds"] or 0),
        window_seconds=float(window["seconds"] or 0),
        run_cost_usd=float(run["cost"] or 0), stage_cost_usd=float(stage_row["cost"] or 0),
        window_cost_usd=float(window["cost"] or 0), cost_unknown=bool(run["unknown"] or 0),
    )


def admit(
    engine_root: Path,
    limits: Limits,
    *,
    run_key: str,
    estimated: int,
    mode: str = SOFT,
    stage: str = "",
    estimated_seconds: float = 0.0,
    estimated_cost_usd: float | None = None,
) -> Decision:
    """Decides whether one call may be made, without making it.

    Pure of side effects on purpose: `reserve` is what commits capacity, and
    keeping the decision separate means a caller can explain a refusal (or show
    a dry-run budget) without consuming any.
    """
    if mode not in MODES:
        raise ValueError(f"Unknown budget mode {mode!r}. Valid: {', '.join(MODES)}")
    if not limits.declared:
        return Decision(allowed=True, mode=mode, reason="no budget declared", estimated=estimated,
                        estimated_seconds=estimated_seconds, estimated_cost_usd=estimated_cost_usd)

    if any((limits.max_run_cost_usd, limits.max_stage_cost_usd, limits.max_window_cost_usd)) and estimated_cost_usd is None and mode != SOFT:
        return Decision(allowed=False, mode=mode, limit="currency_unknown", estimated=estimated,
                        estimated_seconds=estimated_seconds,
                        reason="provider did not supply a cost estimate; strict currency budgets fail closed")

    current = usage(engine_root, limits, run_key=run_key, stage=stage)
    checks = (
        ("max_stage_tokens", estimated, limits.max_stage_tokens),
        ("max_run_tokens", current.run_tokens + estimated, limits.max_run_tokens),
        ("max_run_calls", current.run_calls + 1, limits.max_run_calls),
        ("max_window_tokens", current.window_tokens + estimated, limits.max_window_tokens),
        ("max_stage_seconds", current.stage_seconds + estimated_seconds, limits.max_stage_seconds),
        ("max_run_seconds", current.run_seconds + estimated_seconds, limits.max_run_seconds),
        ("max_stage_cost_usd", current.stage_cost_usd + (estimated_cost_usd or 0), limits.max_stage_cost_usd),
        ("max_run_cost_usd", current.run_cost_usd + (estimated_cost_usd or 0), limits.max_run_cost_usd),
        ("max_window_cost_usd", current.window_cost_usd + (estimated_cost_usd or 0), limits.max_window_cost_usd),
    )
    for name, would_total, ceiling in checks:
        if ceiling and would_total > ceiling:
            return Decision(
                allowed=mode == SOFT,
                mode=mode,
                limit=name,
                estimated=estimated,
                would_total=would_total,
                ceiling=ceiling,
                estimated_seconds=estimated_seconds,
                estimated_cost_usd=estimated_cost_usd,
                reason=(
                    f"{name} would reach {would_total:,} of {ceiling:,} "
                    f"(this call is estimated at {estimated:,} tokens)"
                ),
            )

    return Decision(
        allowed=True,
        mode=mode,
        estimated=estimated,
        would_total=current.run_tokens + estimated,
        ceiling=limits.max_run_tokens,
        reason="within budget",
        estimated_seconds=estimated_seconds,
        estimated_cost_usd=estimated_cost_usd,
    )


def duration_estimate(limits: Limits, *, default: float = 60.0) -> float:
    ceilings = [value for value in (limits.max_stage_seconds, limits.max_run_seconds) if value]
    return min([default, *ceilings]) if ceilings else 0.0


def validate(engine_root: Path, limits: Limits, *, run_key: str, stage: str = "",
             mode: str = SOFT) -> Decision:
    current = usage(engine_root, limits, run_key=run_key, stage=stage)
    if any((limits.max_run_cost_usd, limits.max_stage_cost_usd, limits.max_window_cost_usd)) and current.cost_unknown and mode != SOFT:
        return Decision(False, mode=mode, limit="currency_unknown",
                        reason="provider cost is unknown; budget failed closed")
    checks = (
        ("max_stage_seconds", current.stage_seconds, limits.max_stage_seconds),
        ("max_run_seconds", current.run_seconds, limits.max_run_seconds),
        ("max_stage_cost_usd", current.stage_cost_usd, limits.max_stage_cost_usd),
        ("max_run_cost_usd", current.run_cost_usd, limits.max_run_cost_usd),
        ("max_window_cost_usd", current.window_cost_usd, limits.max_window_cost_usd),
    )
    for name, total, ceiling in checks:
        if ceiling and total > ceiling:
            return Decision(False, mode=mode, limit=name, would_total=total, ceiling=ceiling,
                            reason=f"{name} consumed {total:.3f} of {ceiling:.3f}")
    return Decision(True, mode=mode, reason="settled usage within budget")


def reserve(
    engine_root: Path,
    *,
    run_key: str,
    estimated: int,
    stage: str = "",
    agent: str = "",
    provider: str = "",
    estimated_seconds: float = 0.0,
    estimated_cost_usd: float | None = None,
) -> int:
    """Commits capacity for a call that is about to be made, and returns the
    reservation id the caller must later settle or release."""
    with connect(engine_root) as con:
        _ensure(con)
        cursor = con.execute(
            "INSERT INTO reservations(run_key, stage, agent, provider, estimated, state, created_at,"
            " estimated_seconds, estimated_cost_usd) VALUES(?,?,?,?,?,?,?,?,?)",
            (run_key, stage, agent, provider, estimated, HELD,
             datetime.now(timezone.utc).isoformat(), estimated_seconds, estimated_cost_usd),
        )
        return int(cursor.lastrowid)


def settle(engine_root: Path, reservation_id: int, actual: int, *, actual_seconds: float | None = None, actual_cost_usd: float | None = None) -> None:
    """Replaces an estimate with what the call really cost.

    Applies to failed calls too, and that is deliberate: a provider that errored
    after processing a 200k-token prompt spent those tokens. Releasing a failed
    call instead of settling it would make failures free, which is precisely
    backwards for a correction loop that retries them.
    """
    with connect(engine_root) as con:
        _ensure(con)
        con.execute(
            "UPDATE reservations SET actual = ?, actual_seconds = ?, actual_cost_usd = ?, state = ?, settled_at = ?"
            " WHERE id = ? AND state = ?",
            (max(0, actual), actual_seconds, actual_cost_usd, SETTLED,
             datetime.now(timezone.utc).isoformat(), reservation_id, HELD),
        )


def release(engine_root: Path, reservation_id: int) -> None:
    """Gives capacity back for a call that never reached a provider — a routing
    error, a refusal, an exception before dispatch. Nothing was spent, so
    nothing should be counted."""
    with connect(engine_root) as con:
        _ensure(con)
        con.execute(
            "UPDATE reservations SET state = ?, settled_at = ? WHERE id = ? AND state = ?",
            (RELEASED, datetime.now(timezone.utc).isoformat(), reservation_id, HELD),
        )


def reconcile(engine_root: Path, *, stale_after_seconds: float = STALE_AFTER_SECONDS) -> int:
    """Reclaims reservations held by runs that are no longer spending.

    A killed worker cannot release its own; left held they would shrink every
    later run's window forever, which is a budget that tightens itself every
    time something crashes. Age is the signal, for the same reason it is for a
    stale heartbeat: a pid is only meaningful on the host that issued it.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)).isoformat()
    with connect(engine_root) as con:
        _ensure(con)
        cursor = con.execute(
            "UPDATE reservations SET state = ?, settled_at = ?"
            " WHERE state = ? AND created_at < ?",
            (RELEASED, datetime.now(timezone.utc).isoformat(), HELD, cutoff),
        )
        return cursor.rowcount


def report(engine_root: Path, limits: Limits, *, run_key: str, mode: str = SOFT) -> Report:
    """The closing figures for one run: reserved, consumed, remaining."""
    with connect(engine_root) as con:
        _ensure(con)
        row = con.execute(
            "SELECT COALESCE(SUM(estimated), 0) AS reserved,"
            " COALESCE(SUM(COALESCE(actual, estimated)), 0) AS consumed,"
            " COALESCE(SUM(estimated_seconds), 0) AS reserved_seconds,"
            " COALESCE(SUM(COALESCE(actual_seconds, estimated_seconds)), 0) AS consumed_seconds,"
            " COALESCE(SUM(estimated_cost_usd), 0) AS reserved_cost,"
            " COALESCE(SUM(COALESCE(actual_cost_usd, estimated_cost_usd)), 0) AS consumed_cost,"
            " SUM(CASE WHEN actual_cost_usd IS NULL AND estimated_cost_usd IS NULL THEN 1 ELSE 0 END) AS cost_unknown,"
            " COUNT(*) AS calls"
            " FROM reservations WHERE run_key = ? AND state <> ?",
            (run_key, RELEASED),
        ).fetchone()
    return Report(
        reserved=int(row["reserved"]),
        consumed=int(row["consumed"]),
        calls=int(row["calls"]),
        limit=limits.max_run_tokens,
        mode=mode,
        reserved_seconds=float(row["reserved_seconds"] or 0),
        consumed_seconds=float(row["consumed_seconds"] or 0),
        reserved_cost_usd=float(row["reserved_cost"] or 0),
        consumed_cost_usd=float(row["consumed_cost"] or 0),
        cost_unknown=bool(row["cost_unknown"] or 0),
        seconds_limit=limits.max_run_seconds,
        cost_limit_usd=limits.max_run_cost_usd,
        stage_cost_limit_usd=limits.max_stage_cost_usd,
        window_cost_limit_usd=limits.max_window_cost_usd,
    )
