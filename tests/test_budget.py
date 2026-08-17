"""Tests for core.jobs.budget — hard limits with reservations (issue #27).

The property that separates this from `core.telemetry.quota` is that it stops
a call rather than steering it, and that it accounts for money not yet spent.
Most of these are about the second: two runs in flight must not each admit a
call the budget can only afford once.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.jobs import budget
from core.jobs.budget import Limits


@pytest.fixture
def engine(tmp_path: Path) -> Path:
    return tmp_path


TIGHT = Limits(max_run_tokens=1000, window_hours=24)


# --- estimation ---


def test_estimation_scales_with_the_prompt() -> None:
    small = budget.estimate_tokens("x" * 100, output_reserve=0)
    large = budget.estimate_tokens("x" * 10_000, output_reserve=0)

    assert large > small * 50


def test_estimation_reserves_room_for_a_response() -> None:
    """A budget counting only the prompt is blind to the larger half of many
    calls, and the response cannot be measured in advance."""
    assert budget.estimate_tokens("") == budget.DEFAULT_OUTPUT_RESERVE


def test_estimation_sums_every_part_of_the_prompt() -> None:
    together = budget.estimate_tokens("a" * 400, "b" * 400, output_reserve=0)
    apart = budget.estimate_tokens("a" * 800, output_reserve=0)

    assert together == apart


# --- admission ---


def test_no_declared_budget_admits_everything(engine: Path) -> None:
    """This module loads for every run, including the interactive ones nobody
    wants gated. Undeclared has to behave as if it were not here."""
    decision = budget.admit(engine, Limits(), run_key="r1", estimated=10**9)

    assert decision.allowed is True
    assert "no budget declared" in decision.reason


def test_a_call_within_budget_is_admitted(engine: Path) -> None:
    assert budget.admit(engine, TIGHT, run_key="r1", estimated=100).allowed is True


def test_soft_mode_reports_an_overrun_without_blocking(engine: Path) -> None:
    decision = budget.admit(engine, TIGHT, run_key="r1", estimated=5000, mode=budget.SOFT)

    assert decision.allowed is True
    assert decision.limit == "max_run_tokens"
    assert "5,000 of 1,000" in decision.reason


def test_strict_mode_refuses_an_overrun(engine: Path) -> None:
    decision = budget.admit(engine, TIGHT, run_key="r1", estimated=5000, mode=budget.STRICT)

    assert decision.allowed is False
    assert decision.ceiling == 1000


def test_local_fallback_refuses_rather_than_spending(engine: Path) -> None:
    """No local adapter exists yet (#37), so there is nothing to fall back to.
    Waiting is the behaviour the criterion asks for when no local profile is
    eligible — quietly continuing on a paid provider is not."""
    decision = budget.admit(
        engine, TIGHT, run_key="r1", estimated=5000, mode=budget.LOCAL_FALLBACK
    )

    assert decision.allowed is False


def test_an_unknown_mode_is_refused(engine: Path) -> None:
    with pytest.raises(ValueError, match="Unknown budget mode"):
        budget.admit(engine, TIGHT, run_key="r1", estimated=1, mode="yolo")


def test_each_limit_is_checked_by_name(engine: Path) -> None:
    limits = Limits(max_stage_tokens=100, max_run_calls=2, max_window_tokens=10**9)

    assert budget.admit(engine, limits, run_key="r", estimated=500, mode=budget.STRICT).limit == "max_stage_tokens"

    budget.reserve(engine, run_key="r", estimated=10)
    budget.reserve(engine, run_key="r", estimated=10)
    assert budget.admit(engine, limits, run_key="r", estimated=10, mode=budget.STRICT).limit == "max_run_calls"


# --- reservations are what make concurrency safe ---


def test_a_held_reservation_counts_against_the_next_admission(engine: Path) -> None:
    budget.reserve(engine, run_key="r1", estimated=900)

    decision = budget.admit(engine, TIGHT, run_key="r1", estimated=200, mode=budget.STRICT)

    assert decision.allowed is False


def test_another_run_in_flight_counts_against_the_window(engine: Path) -> None:
    """The whole reason reservations exist. Two jobs each looking only at
    completed calls would both admit a call the budget affords once."""
    limits = Limits(max_window_tokens=1000, window_hours=24)
    budget.reserve(engine, run_key="other-job", estimated=900)

    decision = budget.admit(engine, limits, run_key="mine", estimated=200, mode=budget.STRICT)

    assert decision.allowed is False
    assert decision.limit == "max_window_tokens"


def test_settling_replaces_the_estimate_with_the_real_figure(engine: Path) -> None:
    reservation = budget.reserve(engine, run_key="r1", estimated=900)

    budget.settle(engine, reservation, 100)

    assert budget.usage(engine, TIGHT, run_key="r1").run_tokens == 100


def test_a_failed_call_still_costs_what_it_spent(engine: Path) -> None:
    """A provider that errored after processing a 200k-token prompt spent those
    tokens. Making failures free is backwards for a loop that retries them."""
    reservation = budget.reserve(engine, run_key="r1", estimated=900)

    budget.settle(engine, reservation, 750)

    assert budget.usage(engine, TIGHT, run_key="r1").run_tokens == 750


def test_releasing_gives_the_capacity_back(engine: Path) -> None:
    reservation = budget.reserve(engine, run_key="r1", estimated=900)

    budget.release(engine, reservation)

    assert budget.usage(engine, TIGHT, run_key="r1").run_tokens == 0
    assert budget.admit(engine, TIGHT, run_key="r1", estimated=900, mode=budget.STRICT).allowed


def test_settling_twice_does_not_double_count(engine: Path) -> None:
    reservation = budget.reserve(engine, run_key="r1", estimated=900)
    budget.settle(engine, reservation, 100)

    budget.settle(engine, reservation, 500)

    assert budget.usage(engine, TIGHT, run_key="r1").run_tokens == 100


def test_a_released_reservation_cannot_be_settled_afterwards(engine: Path) -> None:
    reservation = budget.reserve(engine, run_key="r1", estimated=900)
    budget.release(engine, reservation)

    budget.settle(engine, reservation, 500)

    assert budget.usage(engine, TIGHT, run_key="r1").run_tokens == 0


# --- reclaiming what a crash left behind ---


def _age(engine: Path, *, seconds: float) -> None:
    from core.jobs.store import connect

    old = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    with connect(engine) as con:
        con.execute("UPDATE reservations SET created_at = ?", (old,))


def test_reconcile_reclaims_reservations_a_dead_run_still_holds(engine: Path) -> None:
    """Left held they would shrink every later run's window forever — a budget
    that tightens itself every time something crashes."""
    budget.reserve(engine, run_key="crashed", estimated=900)
    _age(engine, seconds=budget.STALE_AFTER_SECONDS + 60)

    assert budget.reconcile(engine) == 1

    assert budget.usage(engine, TIGHT, run_key="crashed").run_tokens == 0


def test_reconcile_leaves_a_live_reservation_alone(engine: Path) -> None:
    """Reclaiming one that is still in flight would let the budget be spent
    twice, so the window is generous — a critical-profile call runs for
    minutes."""
    budget.reserve(engine, run_key="live", estimated=900)

    assert budget.reconcile(engine) == 0
    assert budget.usage(engine, TIGHT, run_key="live").run_tokens == 900


def test_reconcile_does_not_touch_a_settled_reservation(engine: Path) -> None:
    reservation = budget.reserve(engine, run_key="done", estimated=900)
    budget.settle(engine, reservation, 500)
    _age(engine, seconds=budget.STALE_AFTER_SECONDS + 60)

    budget.reconcile(engine)

    assert budget.usage(engine, TIGHT, run_key="done").run_tokens == 500


def test_the_window_only_counts_recent_reservations(engine: Path) -> None:
    reservation = budget.reserve(engine, run_key="old", estimated=900)
    budget.settle(engine, reservation, 900)
    _age(engine, seconds=48 * 3600)

    assert budget.usage(engine, Limits(window_hours=24), run_key="new").window_tokens == 0


# --- the closing report ---


def test_the_report_shows_reserved_consumed_and_remaining(engine: Path) -> None:
    first = budget.reserve(engine, run_key="r1", estimated=400)
    budget.settle(engine, first, 250)
    budget.reserve(engine, run_key="r1", estimated=300)

    report = budget.report(engine, TIGHT, run_key="r1", mode=budget.STRICT)

    assert report.reserved == 700  # what was set aside
    assert report.consumed == 550  # 250 real + 300 still in flight
    assert report.calls == 2
    assert report.remaining == 450
    assert "mode strict" in report.line()


def test_the_report_says_so_when_nothing_is_capped(engine: Path) -> None:
    reservation = budget.reserve(engine, run_key="r1", estimated=400)
    budget.settle(engine, reservation, 250)

    assert "no limit declared" in budget.report(engine, Limits(), run_key="r1").line()


def test_time_reservations_bound_concurrent_runs(engine: Path) -> None:
    limits = Limits(max_run_seconds=60)
    budget.reserve(engine, run_key="r", estimated=1, estimated_seconds=50)
    decision = budget.admit(
        engine, limits, run_key="r", estimated=1, estimated_seconds=20, mode=budget.STRICT
    )
    assert decision.allowed is False
    assert decision.limit == "max_run_seconds"


def test_stage_time_is_scoped_to_stage(engine: Path) -> None:
    limits = Limits(max_stage_seconds=30)
    budget.reserve(engine, run_key="r", stage="build", estimated=1, estimated_seconds=25)
    assert budget.admit(
        engine, limits, run_key="r", stage="build", estimated=1,
        estimated_seconds=10, mode=budget.STRICT
    ).allowed is False
    assert budget.admit(
        engine, limits, run_key="r", stage="test", estimated=1,
        estimated_seconds=10, mode=budget.STRICT
    ).allowed is True


def test_a_currency_ceiling_does_not_refuse_a_call_nobody_can_price(engine: Path) -> None:
    """No provider here prices a request from its prompt.

    So an admission that demanded a cost estimate demanded something that never
    arrives, and a declared USD ceiling refused every call in strict mode —
    which is not a budget, it is an outage.
    """
    for mode in (budget.SOFT, budget.STRICT, budget.LOCAL_FALLBACK):
        decision = budget.admit(
            engine, Limits(max_run_cost_usd=1), run_key=f"r-{mode}", estimated=1, mode=mode
        )
        assert decision.allowed is True, f"{mode} refused a call it could not price"


def test_a_provider_that_should_have_priced_a_call_and_did_not_fails_closed(engine: Path) -> None:
    """The anomaly worth refusing: cost was expected, and none came back.

    Counting that call at zero would let the ceiling be crossed by a total it
    already knows is short, so strict mode stops rather than undercount.
    """
    reservation = budget.reserve(engine, run_key="r", estimated=1, cost_expected=True)
    budget.settle(engine, reservation, 1, actual_cost_usd=None)

    decision = budget.admit(
        engine, Limits(max_run_cost_usd=1), run_key="r", estimated=1, mode=budget.STRICT
    )
    assert decision.allowed is False
    assert decision.limit == "currency_unknown"


def test_a_provider_that_cannot_price_a_call_is_not_an_anomaly(engine: Path) -> None:
    """`codex_cli` reports tokens and no price. That is how it works, not a gap."""
    reservation = budget.reserve(engine, run_key="r", estimated=1, cost_expected=False)
    budget.settle(engine, reservation, 1, actual_cost_usd=None)

    assert budget.admit(
        engine, Limits(max_run_cost_usd=1), run_key="r", estimated=1, mode=budget.STRICT
    ).allowed is True
    assert budget.validate(
        engine, Limits(max_run_cost_usd=1), run_key="r", mode=budget.STRICT
    ).allowed is True


def test_soft_currency_budget_reports_unknown_cost(engine: Path) -> None:
    reservation = budget.reserve(engine, run_key="r", estimated=1, cost_expected=True)
    budget.settle(engine, reservation, 1, actual_seconds=2.5)
    report = budget.report(engine, Limits(max_run_cost_usd=1), run_key="r")
    assert report.cost_unknown is True
    assert "unknown" in report.line()


def test_currency_settlement_is_checked_against_the_ceiling(engine: Path) -> None:
    reservation = budget.reserve(engine, run_key="r", estimated=1, estimated_cost_usd=0.5)
    budget.settle(engine, reservation, 1, actual_cost_usd=1.25)
    decision = budget.validate(
        engine, Limits(max_run_cost_usd=1), run_key="r", mode=budget.STRICT
    )
    assert decision.allowed is False
    assert decision.limit == "max_run_cost_usd"
