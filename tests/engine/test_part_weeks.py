# SPDX-License-Identifier: AGPL-3.0-or-later
"""The engine on a period with a part week (D120): weekly caps count the whole calendar week,
including shifts on the neighbouring period, and weekly goals are scaled. Small real solves."""

from __future__ import annotations

from datetime import date

from domain.availability import AvailabilityStatus, WeeklyAvailability
from domain.calendar import SAT, OperatingHours
from domain.engine import Mode, SolveRequest, run
from domain.rules import CapScope, CoverageRules, HourCap, HourTarget, Rules
from domain.schedule import Period, Person, ScheduleContext, Shift, minutes_in_period
from domain.timeutil import Interval
from domain.validation import Category

# Wed 20 to Fri 22 Jan 2027: the end of the week that starts Sat 16 Jan.
PART = Period(date(2027, 1, 20), date(2027, 1, 22), SAT)
HOURS = [OperatingHours(date(2026, 1, 1), None, {wd: [Interval(540, 1020)] for wd in range(5)})]  # 9-5
FREE = WeeklyAvailability(AvailabilityStatus.ENTERED)
MONDAY_BEFORE = date(2027, 1, 18)  # same calendar week, on the neighbouring period


def person(pid):
    return Person(pid, f"P{pid}", FREE, {d: frozenset() for d in PART.days})


def solve(rules, elsewhere=()):
    ctx = ScheduleContext(PART, HOURS, [], {1: person(1), 2: person(2)}, rules, [], elsewhere=list(elsewhere))
    return ctx, run(ctx, SolveRequest(Mode.GENERATE, time_limit=10, workers=2)).options[0]


def test_weekly_cap_counts_the_neighbouring_periods_days_in_the_same_week():
    rules = Rules(coverage=CoverageRules(default=1), caps=[HourCap(CapScope.WEEK, 8 * 60, employee_id=1)])
    monday = Shift(99, 1, MONDAY_BEFORE, Interval(540, 780))  # 4h already that week
    ctx, option = solve(rules, [monday])
    assert minutes_in_period(option.shifts, ctx.period).get(1, 0) <= 4 * 60
    assert [i for i in option.report.issues if i.category == Category.CAP] == []


def test_without_the_neighbouring_shift_the_whole_cap_is_free():
    rules = Rules(coverage=CoverageRules(default=1), caps=[HourCap(CapScope.WEEK, 8 * 60, employee_id=1)])
    ctx, option = solve(rules)
    # Person 1 now gets more than the 4h left above (fairness spreads load off person 2), and the
    # cap still holds.
    assert 4 * 60 < minutes_in_period(option.shifts, ctx.period).get(1, 0) <= 8 * 60
    assert [i for i in option.report.issues if i.category == Category.CAP] == []


def test_hour_target_in_a_part_week_is_scaled_to_its_share():
    # Wed-Fri hold 24 of the week's 40 open hours, so a 10h target asks for 6h here.
    rules = Rules(coverage=CoverageRules(default=1), targets=[HourTarget(1, 10 * 60, priority=5)])
    ctx, option = solve(rules)
    assert ctx.weekly_goal(10 * 60, 0) == 6 * 60
    assert minutes_in_period(option.shifts, ctx.period).get(1, 0) >= 6 * 60
    assert not any("target" in u.message for u in option.explanation.unmet)
