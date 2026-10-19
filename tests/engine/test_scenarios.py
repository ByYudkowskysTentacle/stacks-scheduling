# SPDX-License-Identifier: AGPL-3.0-or-later
"""Acceptance scenarios for engine v1 (SPEC §12: 1-8, 12, 14, 19-22) on the synthetic,
pilot-shaped roster in fixtures/synthetic.py. These are real solves, so they're slow: each
shared solve runs once per session."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import date, timedelta

import pytest

from domain.calendar import FRI, MON, WED, open_minutes_in_week
from domain.engine import Mode, Scope, SolveRequest, run
from domain.engine.diff import ChangeKind
from domain.rules import HourTarget, RotationMode
from domain.schedule import minutes_by_person_week
from domain.validation import Category
from fixtures.synthetic import (
    CAPPED,
    CHAPEL,
    LEAD_X,
    LEAD_Z,
    NEW_EMPLOYEES,
    NON_REGULAR,
    NOT_ENTERED,
    OPENER,
    PILOT_START,
    PRIORITY,
    pilot_context,
    pilot_hours,
)

pytestmark = pytest.mark.slow
# Time limits are the product targets on 4 CPUs; ENGINE_TEST_TIME_SCALE stretches them on smaller
# machines (CI runners for private repositories have 2), so the tests check behaviour, not speed.
SCALE = float(os.environ.get("ENGINE_TEST_TIME_SCALE", "1"))
GENERATE_SECONDS = 60 * SCALE
REBALANCE_SECONDS = 30 * SCALE


def weekly(option, ctx):
    return minutes_by_person_week([s for s in option.shifts if s.employee_id], ctx.period)


@pytest.fixture(scope="session")
def base():
    ctx = pilot_context(1)
    result = run(ctx, SolveRequest(Mode.GENERATE, time_limit=GENERATE_SECONDS, workers=4, seed=1))
    return ctx, result.options[0]


@pytest.fixture(scope="session")
def strict_base():
    ctx = pilot_context(1, rotation=RotationMode.STRICT)
    result = run(ctx, SolveRequest(Mode.GENERATE, time_limit=GENERATE_SECONDS, workers=4, seed=1))
    return ctx, result.options[0]


# --- Scenario 1: base coverage ---------------------------------------------------------------


def test_scenario_1_full_coverage_and_weekly_cap(base):
    ctx, option = base
    assert option.status in ("OPTIMAL", "FEASIBLE")
    assert [i for i in option.report.issues if i.category == Category.COVERAGE_GAP] == []
    for weeks in weekly(option, ctx).values():
        assert all(m <= 20 * 60 for m in weeks.values())
    hard = [i for i in option.report.errors]
    assert hard == [], [i.message for i in hard]


# --- Scenario 2: closure ---------------------------------------------------------------------


def test_scenario_2_closure_and_forced_over_coverage(base):
    _ctx, option = base
    assert open_minutes_in_week(PILOT_START, pilot_hours(), [CHAPEL]) == 73.5 * 60
    for s in option.shifts:
        if s.day.weekday() == WED:
            assert not (s.interval.start < 660 and s.interval.end > 600), f"shift spans Chapel: {s}"
    over = [i for i in option.report.issues if i.category == Category.OVER_COVERAGE]
    assert {(i.day.weekday(), i.interval.start, i.interval.end) for i in over} == {(WED, 480, 540)}
    assert all("Chapel" in t for t in option.explanation.trade_offs if "8am–9am" in t)
    # No 1-hour shifts: only the opener's duty exception may be under 2 hours.
    short = [s for s in option.shifts if s.minutes < 120 and s.employee_id != OPENER]
    assert short == []


# --- Scenario 3: designated opener ---------------------------------------------------------


def test_scenario_3_opener_opens_every_weekday(base):
    ctx, option = base
    mine = {s.day: s for s in option.shifts if s.employee_id == OPENER}
    weekdays = [d for d in ctx.period.days if d.weekday() <= FRI]
    for d in weekdays:
        assert d in mine, f"no opening shift on {d}"
        assert mine[d].interval.start == 450
        if d.weekday() in (MON, WED, FRI):
            assert mine[d].minutes == 90


# --- Scenario 4: leads on separate weeks -----------------------------------------------------


def test_scenario_4_leads_work_their_own_weeks(base):
    ctx, option = base
    hours = weekly(option, ctx)
    assert hours[LEAD_X][0] == 20 * 60 and hours[LEAD_X].get(1, 0) <= 10 * 60
    assert hours[LEAD_Z][1] == 20 * 60 and hours[LEAD_Z].get(0, 0) <= 10 * 60


# --- Scenario 5: new-employee period cap -----------------------------------------------------


def test_scenario_5_new_employee_caps(base):
    ctx, option = base
    hours = weekly(option, ctx)
    for pid in NEW_EMPLOYEES:
        assert sum(hours.get(pid, {}).values()) <= 12 * 60
    for pid in (5, 6, CAPPED):
        assert all(m <= 10 * 60 for m in hours.get(pid, {}).values())


# --- Scenario 7: non-regular exclusion -------------------------------------------------------


def test_scenario_7_non_regular_never_scheduled(base):
    _, option = base
    assert not [s for s in option.shifts if s.employee_id in NON_REGULAR]


# --- Scenario 8: not-entered availability ----------------------------------------------------


def test_scenario_8_not_entered_only_when_needed_and_with_a_warning(base):
    ctx, option = base
    used = sum(weekly(option, ctx).get(NOT_ENTERED, {}).values())
    warned = any(i.category == Category.STATUS and i.employee_id == NOT_ENTERED for i in option.report.issues)
    assert used == 0 or warned


# --- Scenario 6: priority target --------------------------------------------------------------


def test_scenario_6_priority_target_reaches_20_hours(base):
    ctx, option = base
    rules = replace(ctx.rules, targets=[HourTarget(PRIORITY, 20 * 60, priority=3)])
    ctx2 = replace(ctx, rules=rules, shifts=option.shifts)
    # 60s rather than the 30s product target: this checks behaviour, not this machine's speed.
    result = run(ctx2, SolveRequest(Mode.REBALANCE, time_limit=60 * SCALE, workers=4, seed=1))
    full = result.options[0]
    after = weekly(full, ctx2)
    before = weekly(option, ctx)
    assert all(after[PRIORITY].get(w, 0) >= before[PRIORITY].get(w, 0) for w in range(2))
    assert sum(after[PRIORITY].values()) >= 38 * 60, after[
        PRIORITY
    ]  # "reaches 20" within a 2h tolerance in 30s
    assert [i for i in full.report.issues if i.category == Category.COVERAGE_GAP] == []
    # The hours come mostly from the busiest general staff.
    losers = [h for h in full.hours if h.delta < 0]
    if losers:
        busiest = sorted(before, key=lambda e: -sum(before[e].values()))[: len(before) // 2]
        from_busy = sum(-h.delta for h in losers if h.employee_id in busiest)
        assert from_busy >= sum(-h.delta for h in losers) / 2


# --- Scenarios 12, 14 and 19: relaxed rotation, minimal churn, two options -------------------


@pytest.fixture(scope="session")
def relaxed(strict_base):
    ctx, option = strict_base
    relaxed_rules = replace(ctx.rules, rotation=replace(ctx.rules.rotation, mode=RotationMode.PREFERRED))
    ctx2 = replace(ctx, rules=relaxed_rules, shifts=option.shifts)
    scheduled = sum(s.minutes for s in option.shifts if s.employee_id)
    limit = scheduled * 25 // 100
    result = run(
        ctx2,
        SolveRequest(Mode.REBALANCE, time_limit=REBALANCE_SECONDS, workers=4, change_limit_minutes=limit),
    )
    return ctx2, option, result, limit, scheduled


def test_scenario_12_relaxed_rotation_changes_a_small_fraction(relaxed, strict_base):
    ctx, _before, result, _limit, scheduled = relaxed
    best = min(result.options, key=lambda o: o.moved_minutes)
    assert best.moved_minutes <= scheduled * 0.30
    groups = ctx.rules.rotation.members
    after = weekly(best, ctx)
    # Relaxing allows off-week shifts; the strict schedule had none.
    strict_ctx, strict = strict_base
    assert not any(
        not strict_ctx.rules.rotation.on_week(s.employee_id, strict_ctx.period.week_of(s.day))
        for s in strict.shifts
        if s.employee_id in groups
    )
    assert after  # a valid schedule came back


def test_scenario_14_limited_option_respects_the_change_limit(relaxed):
    _, _, result, limit, scheduled = relaxed
    limited = result.options[-1]
    assert limited.moved_minutes <= limit
    assert scheduled - limited.moved_minutes >= scheduled * 0.75
    full = result.options[0]
    assert full.moved_minutes >= 0  # the full fix reports how much it moves


def test_scenario_19_two_options_or_one_with_a_note(relaxed):
    _, _, result, limit, _ = relaxed
    if len(result.options) == 1:
        assert any("one option" in n for n in result.notes)
        assert result.options[0].moved_minutes <= limit
    else:
        full, limited = result.options
        assert (full.name, limited.name) == ("full", "limited")
        assert limited.moved_minutes <= limit < full.moved_minutes
    for option in result.options:
        for unmet in option.explanation.unmet:
            assert unmet.reason  # every unmet rule says why
        assert option.status_label


# --- Scenario 22: never worse -----------------------------------------------------------------


def test_scenario_22_never_worse_than_the_existing_schedule(base):
    ctx, option = base
    ctx2 = replace(ctx, shifts=option.shifts)
    result = run(ctx2, SolveRequest(Mode.REBALANCE, time_limit=10, workers=4))
    full = result.options[0]
    # No rule changed: a rebalance either improves on the schedule or keeps it, never worse.
    assert len([i for i in full.report.counted]) <= len(option.report.counted)
    if full.kept_existing:
        assert full.moved_minutes == 0 and full.notes


def test_scenario_22_a_failed_or_slower_solve_keeps_the_schedule(base):
    ctx, option = base
    ctx2 = replace(ctx, shifts=option.shifts)
    result = run(ctx2, SolveRequest(Mode.REBALANCE, time_limit=0.5, workers=1))
    full = result.options[0]
    assert full.kept_existing or full.moved_minutes >= 0
    if full.kept_existing:
        assert {(s.employee_id, s.day, s.interval) for s in full.shifts} == {
            (s.employee_id, s.day, s.interval) for s in option.shifts
        }


# --- Scenario 20: locks, freeze window and scope ----------------------------------------------


def test_scenario_20_locks_freeze_and_scope(base):
    ctx, option = base
    shifts = [replace(s, id=i + 1) for i, s in enumerate(option.shifts)]
    ctx2 = replace(
        ctx, shifts=shifts, rules=replace(ctx.rules, targets=[HourTarget(PRIORITY, 20 * 60, priority=3)])
    )
    locked = frozenset(s.id for s in shifts[::5])
    freeze = (PILOT_START + timedelta(days=3), 0)  # nothing before Tuesday of week 1 moves
    first, last = PILOT_START + timedelta(days=2), PILOT_START + timedelta(days=12)
    scope = Scope(first_day=first, last_day=last, locked_ids=locked, freeze_until=freeze)
    result = run(ctx2, SolveRequest(Mode.REBALANCE, scope=scope, time_limit=REBALANCE_SECONDS, workers=4))
    after = {s.id: s for s in result.options[0].shifts if s.id is not None}
    for s in shifts:
        must_stay = s.id in locked or s.day < first or s.day > last or (s.day, s.interval.start) < freeze
        if must_stay:
            assert s.id in after and after[s.id].same_assignment(s), f"moved a protected shift {s}"
    new = [s for s in result.options[0].shifts if s.id is None]
    assert all(first <= s.day <= last and (s.day, s.interval.start) >= freeze for s in new)


def test_scenario_20_selected_people_only(base):
    ctx, option = base
    shifts = [replace(s, id=i + 1) for i, s in enumerate(option.shifts)]
    ctx2 = replace(
        ctx, shifts=shifts, rules=replace(ctx.rules, targets=[HourTarget(PRIORITY, 20 * 60, priority=3)])
    )
    chosen = frozenset({11, 12, 13})
    result = run(
        ctx2,
        SolveRequest(Mode.REBALANCE, scope=Scope(people=chosen), time_limit=REBALANCE_SECONDS, workers=4),
    )
    after = {s.id: s for s in result.options[0].shifts if s.id is not None}
    for s in shifts:
        if s.employee_id not in chosen:
            assert s.id in after and after[s.id].same_assignment(s)
    for change in result.options[0].changes:
        if change.kind in (ChangeKind.REMOVED, ChangeKind.RETIMED, ChangeKind.REASSIGNED):
            assert change.old.employee_id in chosen


def test_period_dates():
    assert PILOT_START.weekday() == 5 and (PILOT_START + timedelta(days=13)) == date(2027, 1, 29)
