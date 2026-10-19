# SPDX-License-Identifier: AGPL-3.0-or-later
"""Engine unit tests on small synthetic problems: candidates, diffs, desired hours
(scenario 21) and a property test over random rosters (CLAUDE.md §5)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from domain.availability import AvailabilityBlock, AvailabilityStatus, BlockKind, WeeklyAvailability
from domain.calendar import MON, SAT, TUE, WED, Closure, OperatingHours
from domain.engine import Mode, SolveRequest, run
from domain.engine.candidates import Scope, build_candidates
from domain.engine.diff import ChangeKind, match, moved_minutes
from domain.rules import (
    CapScope,
    CoverageMode,
    CoverageOverride,
    CoverageRules,
    DesiredHours,
    HourCap,
    OpenerDuty,
    Rotation,
    RotationMode,
    Rules,
    TagRule,
    TagRuleKind,
)
from domain.schedule import Period, Person, ScheduleContext, Shift, minutes_by_person_week
from domain.timeutil import Interval
from domain.validation import Category

START = date(2027, 1, 16)
MONDAY = date(2027, 1, 18)
WEDNESDAY = date(2027, 1, 20)
HOURS = [
    OperatingHours(date(2026, 1, 1), None, {wd: [Interval(480, 1020)] for wd in range(5)})
]  # weekdays 8-5
FREE = WeeklyAvailability(AvailabilityStatus.ENTERED)


def person(pid, availability=FREE, auto=True, tags=()):
    period = Period(START, 1, SAT)
    return Person(
        pid, f"P{pid}", availability, {d: frozenset(tags) for d in period.days}, auto_schedulable=auto
    )


def context(people, rules=None, weeks=1, closures=(), shifts=()):
    return ScheduleContext(
        Period(START, weeks, SAT),
        HOURS,
        list(closures),
        {p.id: p for p in people},
        rules or Rules(),
        list(shifts),
    )


# --- Candidates -----------------------------------------------------------------------------


def test_candidates_respect_availability_closures_and_steps():
    busy = WeeklyAvailability(
        AvailabilityStatus.ENTERED, (AvailabilityBlock(MON, BlockKind.UNAVAILABLE, Interval(540, 590)),)
    )
    chapel = Closure(reason="Chapel", weekday=WED, interval=Interval(600, 660))
    ctx = context([person(1, busy)], closures=[chapel])
    cands = build_candidates(ctx, Scope()).candidates
    monday = [c for c in cands if c.day == MONDAY]
    assert all(not c.interval.overlaps(Interval(540, 600)) for c in monday)  # 9:50 rounds up to 10:00
    wednesday = [c for c in cands if c.day == WEDNESDAY]
    assert all(not c.interval.overlaps(Interval(600, 660)) for c in wednesday)
    assert all(c.start % 30 == 0 and 120 <= c.minutes <= 240 for c in cands)


def test_non_regular_and_strict_off_weeks_get_no_candidates():
    rules = Rules(rotation=Rotation(RotationMode.STRICT, {2: "B"}))
    ctx = context([person(1, auto=False), person(2)], rules=rules, weeks=2)
    cands = build_candidates(ctx, Scope()).candidates
    assert not [c for c in cands if c.employee_id == 1]
    assert {ctx.period.week_of(c.day) for c in cands if c.employee_id == 2} == {1}


def test_opener_duty_adds_the_short_opening_shift():
    rules = Rules(openers=[OpenerDuty(1, frozenset({MON}), frozenset({MON}), 90)])
    cands = build_candidates(context([person(1)], rules), Scope()).candidates
    assert any(c.day == MONDAY and c.start == 480 and c.minutes == 90 and c.duty for c in cands)


def test_frozen_and_out_of_scope_shifts_are_fixed():
    existing = [Shift(10, 1, MONDAY, Interval(480, 600)), Shift(11, 1, WEDNESDAY, Interval(480, 600))]
    scope = Scope(freeze_until=(date(2027, 1, 19), 0))
    cands = build_candidates(context([person(1)], shifts=existing), scope, existing).candidates
    fixed = [c for c in cands if c.fixed]
    assert [(c.existing_id, c.day) for c in fixed] == [(10, MONDAY)]
    assert not [c for c in cands if c.day == MONDAY and not c.fixed]  # nothing new inside the freeze


# --- Diffs ----------------------------------------------------------------------------------


def test_match_keeps_ids_for_kept_retimed_and_reassigned_shifts():
    old = [
        Shift(1, 1, MONDAY, Interval(480, 600)),
        Shift(2, 2, MONDAY, Interval(600, 720)),
        Shift(3, 3, MONDAY, Interval(720, 840)),
        Shift(4, 4, MONDAY, Interval(840, 960)),
    ]
    new = [
        Shift(None, 1, MONDAY, Interval(480, 600)),  # kept
        Shift(None, 2, MONDAY, Interval(630, 750)),  # retimed
        Shift(None, 5, MONDAY, Interval(720, 840)),  # reassigned
        Shift(None, 6, MONDAY, Interval(900, 1020)),  # added (4's shift removed)
    ]
    result, changes = match(old, new)
    kinds = {c.kind: (c.old.id if c.old else None) for c in changes}
    assert (
        kinds[ChangeKind.KEPT] == 1 and kinds[ChangeKind.RETIMED] == 2 and kinds[ChangeKind.REASSIGNED] == 3
    )
    assert kinds[ChangeKind.REMOVED] == 4 and ChangeKind.ADDED in kinds
    assert {s.id for s in result} == {1, 2, 3, None}


def test_hours_moved_counts_retimes_partially():
    old = [Shift(1, 1, MONDAY, Interval(480, 600)), Shift(2, 2, MONDAY, Interval(600, 720))]
    new = [Shift(1, 1, MONDAY, Interval(510, 630)), Shift(2, 3, MONDAY, Interval(600, 720))]
    # Retime loses 30 minutes of overlap; the reassignment counts in full (D33).
    assert moved_minutes(old, new) == 30 + 120


# --- Scenario 21: desired hours ---------------------------------------------------------------


def test_scenario_21_work_study_first_within_the_funding_cap():
    # Coverage of 1 on Monday and Tuesday 8-4: 16 hours to share. Two students each want 12
    # approved hours a week, more than there is. The work-study student gets hours first, up to
    # their funding cap of 10 hours. Exact coverage, so extra hours would be over-coverage.
    window = Interval(480, 960)
    rules = Rules(
        coverage=CoverageRules(
            default=0, mode=CoverageMode.EXACT, overrides=[CoverageOverride(frozenset({MON, TUE}), window, 1)]
        ),
        caps=[HourCap(CapScope.PERIOD, 600, employee_id=1, label="funding cap")],
        desired=[DesiredHours(1, 12 * 60, work_study=True), DesiredHours(2, 12 * 60)],
    )
    ctx = context([person(1), person(2)], rules)
    option = run(ctx, SolveRequest(Mode.GENERATE, time_limit=10, workers=2)).options[0]
    hours = minutes_by_person_week([s for s in option.shifts if s.employee_id], ctx.period)
    fws, other = sum(hours.get(1, {}).values()), sum(hours.get(2, {}).values())
    assert fws == 600  # up to the funding cap, never past it
    assert other == 16 * 60 - 600
    assert not [i for i in option.report.issues if i.category == Category.COVERAGE_GAP]


def test_desired_hours_go_to_whoever_is_furthest_below():
    window = Interval(480, 960)
    rules = Rules(
        coverage=CoverageRules(
            default=0, mode=CoverageMode.EXACT, overrides=[CoverageOverride(frozenset({MON, TUE}), window, 1)]
        ),
        desired=[DesiredHours(1, 12 * 60), DesiredHours(2, 12 * 60)],
    )
    ctx = context([person(1), person(2)], rules)
    option = run(ctx, SolveRequest(Mode.GENERATE, time_limit=10, workers=2)).options[0]
    hours = minutes_by_person_week([s for s in option.shifts if s.employee_id], ctx.period)
    a, b = sum(hours.get(1, {}).values()), sum(hours.get(2, {}).values())
    assert abs(a - b) <= 120  # split roughly evenly rather than one getting everything


# --- New-employee rule, both readings (D96) -------------------------------------------------


def _new_employees_only(kind):
    rule = TagRule("New employee", kind, hard=True)
    window = Interval(480, 720)
    rules = Rules(
        coverage=CoverageRules(default=0, overrides=[CoverageOverride(frozenset({MON}), window, 1)]),
        tag_rules=[rule],
    )
    people = [person(1, tags={"New employee"}), person(2, tags={"New employee"})]
    return run(context(people, rules), SolveRequest(Mode.GENERATE, time_limit=8, workers=2)).options[0]


def test_strict_reading_leaves_a_gap_rather_than_two_new_employees_alone():
    option = _new_employees_only(TagRuleKind.NOT_ALONE)
    categories = {i.category for i in option.report.issues}
    assert Category.TAG_RULE not in categories
    assert Category.COVERAGE_GAP in categories  # no one without the tag exists to pair with


def test_loose_reading_pairs_new_employees_and_covers():
    option = _new_employees_only(TagRuleKind.NOT_SOLO)
    categories = {i.category for i in option.report.issues}
    assert Category.TAG_RULE not in categories and Category.COVERAGE_GAP not in categories


# --- Property test: every result satisfies the hard rules or says which it couldn't -----------


@st.composite
def rosters(draw):
    n = draw(st.integers(min_value=2, max_value=5))
    people = []
    for pid in range(1, n + 1):
        blocks = []
        for wd in draw(st.lists(st.integers(min_value=0, max_value=4), max_size=3, unique=True)):
            start = draw(st.integers(min_value=480, max_value=900))
            end = draw(st.integers(min_value=start + 15, max_value=1020))
            blocks.append(AvailabilityBlock(wd, BlockKind.UNAVAILABLE, Interval(start, end)))
        status = (
            AvailabilityStatus.NOT_ENTERED if draw(st.booleans()) and pid == n else AvailabilityStatus.ENTERED
        )
        people.append(person(pid, WeeklyAvailability(status, tuple(blocks))))
    cap = draw(st.sampled_from([None, 300, 600]))
    rules = Rules(
        coverage=CoverageRules(default=draw(st.integers(min_value=1, max_value=2))),
        caps=[HourCap(CapScope.WEEK, cap)] if cap else [],
    )
    return context(people, rules)


@settings(max_examples=6, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(rosters())
def test_generated_schedules_never_break_hard_rules(ctx):
    option = run(ctx, SolveRequest(Mode.GENERATE, time_limit=3, workers=2)).options[0]
    hard = {Category.AVAILABILITY, Category.SHAPE, Category.CAP, Category.DOUBLE_BOOKING, Category.CLOSED}
    broken = [i for i in option.report.issues if i.category in hard]
    assert broken == [], [i.message for i in broken]
    assert all(ctx.people[s.employee_id].auto_schedulable for s in option.shifts if s.employee_id)


def test_rebalance_after_cap_cut_moves_hours_and_explains(tmp_path=None):
    ctx = context([person(1), person(2), person(3)], Rules(coverage=CoverageRules(default=1)))
    base = run(ctx, SolveRequest(Mode.GENERATE, time_limit=5, workers=2)).options[0]
    shifts = [replace(s, id=i + 1) for i, s in enumerate(base.shifts)]
    busiest = max(minutes_by_person_week(shifts, ctx.period).items(), key=lambda kv: sum(kv[1].values()))[0]
    rules = Rules(coverage=CoverageRules(default=1), caps=[HourCap(CapScope.WEEK, 240, employee_id=busiest)])
    result = run(
        replace(ctx, rules=rules, shifts=shifts), SolveRequest(Mode.REBALANCE, time_limit=5, workers=2)
    )
    after = minutes_by_person_week([s for s in result.options[0].shifts if s.employee_id], ctx.period)
    assert sum(after.get(busiest, {}).values()) <= 240
    assert result.options[0].hours  # who gains and who loses (CLAUDE.md §2.5)
    assert date(2027, 1, 16) + timedelta(days=6) == ctx.period.end
