# SPDX-License-Identifier: AGPL-3.0-or-later
"""The validation suite (SPEC §8): one failing and one passing case per rule (CLAUDE.md §5)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from domain.availability import AvailabilityBlock, AvailabilityStatus, BlockKind, WeeklyAvailability
from domain.calendar import MON, SAT
from domain.rules import (
    CapScope,
    CoverageMode,
    CoverageOverride,
    CoverageRules,
    HourCap,
    Rules,
    ShapeException,
    TagRule,
    TagRuleKind,
)
from domain.schedule import Period, Person, ScheduleContext, Shift
from domain.timeutil import Interval
from domain.validation import Category, Severity, validate
from tests.domain.test_calendar import CHAPEL, PILOT

START = date(2027, 1, 16)  # a Saturday
MONDAY = date(2027, 1, 18)
WEDNESDAY = date(2027, 1, 20)
PERIOD = Period.of_weeks(START, 2, SAT)
FREE = WeeklyAvailability(AvailabilityStatus.ENTERED)


def person(pid, name=None, availability=FREE, tags=(), tag_days=None):
    days = tag_days if tag_days is not None else PERIOD.days
    return Person(pid, name or f"P{pid}", availability, {d: frozenset(tags) for d in days})


def shift(pid, day, start, end, sid=None):
    return Shift(sid, pid, day, Interval(start, end))


def ctx(shifts, people=None, rules=None, published=None, weeks=1):
    """A one-week context by default, so single-day tests aren't swamped by the second week."""
    people = people or {p: person(p) for p in {s.employee_id for s in shifts if s.employee_id}}
    period = Period.of_weeks(START, weeks, SAT)
    return ScheduleContext(period, [PILOT], [CHAPEL], people, rules or Rules(), shifts, published)


def only(report, category):
    return [i for i in report.issues if i.category == category]


def single_day_rules(**kw):
    """Coverage of 1 on Monday 9-1 only, so one-day tests aren't swamped by gaps elsewhere."""
    return Rules(
        coverage=CoverageRules(
            default=0, overrides=[CoverageOverride(frozenset({MON}), Interval(540, 780), 1)]
        ),
        **kw,
    )


# --- Coverage ------------------------------------------------------------------------------


def test_coverage_gap_reported_with_size():
    r = validate(ctx([shift(1, MONDAY, 540, 660)], rules=single_day_rules()))
    gaps = only(r, Category.COVERAGE_GAP)
    assert [(g.day, g.interval, g.minutes) for g in gaps] == [(MONDAY, Interval(660, 780), 120)]
    assert gaps[0].message == "Mon 18 Jan 11am–1pm: 1 person short"


def test_full_coverage_reports_zero_issues():
    r = validate(ctx([shift(1, MONDAY, 540, 780)], rules=single_day_rules()))
    assert r.counted == [] and r.summary() == "0 issues"


def test_over_coverage_in_exact_mode_only():
    shifts = [shift(1, MONDAY, 540, 780), shift(2, MONDAY, 540, 660)]
    assert [
        i.minutes for i in only(validate(ctx(shifts, rules=single_day_rules())), Category.OVER_COVERAGE)
    ] == [120]
    minimum = Rules(coverage=CoverageRules(0, CoverageMode.MINIMUM, single_day_rules().coverage.overrides))
    assert only(validate(ctx(shifts, rules=minimum)), Category.OVER_COVERAGE) == []


def test_shift_during_closure_is_an_error():
    r = validate(ctx([shift(1, WEDNESDAY, 540, 720)], rules=Rules(coverage=CoverageRules(default=0))))
    closed = only(r, Category.CLOSED)
    assert len(closed) == 1 and closed[0].minutes == 60 and "10am–11am" in closed[0].message


def test_shift_inside_open_hours_is_not_closed():
    r = validate(ctx([shift(1, WEDNESDAY, 660, 840)], rules=Rules(coverage=CoverageRules(default=0))))
    assert only(r, Category.CLOSED) == []


def test_pilot_coverage_target_counts_every_open_slot():
    # Exactly 2 on duty at all 73.5 open hours, both weeks, with nobody scheduled: all gaps.
    r = validate(ctx([], rules=Rules(), weeks=2))
    assert sum(i.minutes for i in only(r, Category.COVERAGE_GAP)) == 2 * 2 * 73.5 * 60


# --- Availability and status ---------------------------------------------------------------


def test_shift_over_a_class_conflicts_using_rounded_times():
    busy = WeeklyAvailability(
        AvailabilityStatus.ENTERED, (AvailabilityBlock(MON, BlockKind.UNAVAILABLE, Interval(540, 590)),)
    )
    people = {1: person(1, "Avery", busy)}
    # A 9:50 class end blocks until 10:00, so a 9:50 start still clashes.
    # The message shows the exact class time.
    issues = only(
        validate(ctx([shift(1, MONDAY, 585, 705)], people, single_day_rules())), Category.AVAILABILITY
    )
    assert len(issues) == 1 and "9am–9:50am" in issues[0].message
    assert (
        only(validate(ctx([shift(1, MONDAY, 600, 720)], people, single_day_rules())), Category.AVAILABILITY)
        == []
    )


def test_not_entered_person_is_flagged_once():
    people = {1: person(1, "Casey", WeeklyAvailability(AvailabilityStatus.NOT_ENTERED))}
    shifts = [shift(1, MONDAY, 540, 660), shift(1, MONDAY + timedelta(days=1), 540, 660)]
    status = only(validate(ctx(shifts, people, single_day_rules())), Category.STATUS)
    assert len(status) == 1 and status[0].severity == Severity.WARNING


def test_needs_review_is_info_only():
    people = {1: person(1, availability=WeeklyAvailability(AvailabilityStatus.NEEDS_REVIEW))}
    status = only(validate(ctx([shift(1, MONDAY, 540, 780)], people, single_day_rules())), Category.STATUS)
    assert [s.severity for s in status] == [Severity.INFO]


# --- Shape and double booking --------------------------------------------------------------


@pytest.mark.parametrize(
    ("shifts", "expected"),
    [
        ([(540, 600)], "shorter than 2h"),
        ([(480, 780)], "longer than 4h"),
        ([(480, 600), (630, 780), (810, 930)], "3 shifts"),
        ([(480, 720), (750, 960)], "more than 6h"),
        ([(480, 600), (615, 735)], "only 15 minutes between shifts"),
        ([(480, 607), (690, 810)], "not on 15-minute boundaries"),
    ],
)
def test_shape_violations(shifts, expected):
    r = validate(ctx([shift(1, MONDAY, a, b) for a, b in shifts], rules=single_day_rules()))
    assert any(expected in i.message for i in only(r, Category.SHAPE)), [i.message for i in r.issues]


def test_good_shape_passes():
    r = validate(ctx([shift(1, MONDAY, 480, 600), shift(1, MONDAY, 630, 780)], rules=single_day_rules()))
    assert only(r, Category.SHAPE) == []


def test_opener_shape_exception():
    rules = single_day_rules(shape_exceptions=[ShapeException(1, frozenset({MON}), 90)])
    assert only(validate(ctx([shift(1, MONDAY, 450, 540)], rules=rules)), Category.SHAPE) == []
    tuesday = MONDAY + timedelta(days=1)
    assert only(validate(ctx([shift(1, tuesday, 450, 540)], rules=rules)), Category.SHAPE) != []


def test_double_booking():
    r = validate(ctx([shift(1, MONDAY, 480, 720), shift(1, MONDAY, 600, 780)], rules=single_day_rules()))
    assert len(only(r, Category.DOUBLE_BOOKING)) == 1


# --- Caps ----------------------------------------------------------------------------------


def week_of_shifts(pid, week, hours_per_day, days=5):
    first_monday = START + timedelta(days=2 + 7 * week)
    return [shift(pid, first_monday + timedelta(days=i), 600, 600 + hours_per_day * 60) for i in range(days)]


def test_weekly_cap():
    rules = Rules(coverage=CoverageRules(default=0), caps=[HourCap(CapScope.WEEK, 20 * 60)])
    at_cap = week_of_shifts(1, 0, 4)  # 4h x 5 days = 20h: allowed
    assert only(validate(ctx(at_cap, rules=rules)), Category.CAP) == []
    saturday = [shift(1, START, 720, 840)]  # 2 more hours in the same week
    over = only(validate(ctx(at_cap + saturday, rules=rules)), Category.CAP)
    assert [i.minutes for i in over] == [120]


def test_personal_weekly_cap_is_tighter_than_default():
    rules = Rules(
        coverage=CoverageRules(default=0),
        caps=[HourCap(CapScope.WEEK, 1200), HourCap(CapScope.WEEK, 600, employee_id=1)],
    )
    issues = only(validate(ctx(week_of_shifts(1, 0, 3), rules=rules)), Category.CAP)
    assert [i.minutes for i in issues] == [300]


def test_new_employee_period_cap_by_tag():
    # Scenario 5: people tagged New employee get at most 12 hours across the period.
    rules = Rules(coverage=CoverageRules(default=0), caps=[HourCap(CapScope.PERIOD, 720, tag="New employee")])
    people = {1: person(1, tags={"New employee"}), 2: person(2)}
    shifts = week_of_shifts(1, 0, 2) + week_of_shifts(1, 1, 2) + week_of_shifts(2, 0, 3)
    issues = only(validate(ctx(shifts, people, rules, weeks=2)), Category.CAP)
    assert [(i.employee_id, i.minutes) for i in issues] == [(1, 480)]


def test_term_cap_counts_hours_outside_the_period():
    rules = Rules(coverage=CoverageRules(default=0), caps=[HourCap(CapScope.TERM, 100 * 60, employee_id=1)])
    c = ScheduleContext(
        PERIOD, [PILOT], [CHAPEL], {1: person(1)}, rules, week_of_shifts(1, 0, 4), None, {1: 90 * 60}
    )
    assert [i.minutes for i in only(validate(c), Category.CAP)] == [600]


# --- Tag rules -----------------------------------------------------------------------------


def test_min_on_duty_tag_rule():
    rule = TagRule(
        "Cataloguer", TagRuleKind.MIN_ON_DUTY, weekdays=frozenset({MON}), interval=Interval(540, 1020)
    )
    rules = Rules(coverage=CoverageRules(default=0), tag_rules=[rule])
    people = {1: person(1, tags={"Cataloguer"}), 2: person(2)}
    r = validate(ctx([shift(1, MONDAY, 540, 780), shift(2, MONDAY, 780, 1020)], people, rules, weeks=2))
    issues = only(r, Category.TAG_RULE)
    assert [(i.interval, i.severity) for i in issues if i.day == MONDAY] == [
        (Interval(780, 1020), Severity.WARNING)
    ]
    week2_monday = MONDAY + timedelta(days=7)
    assert any(i.day == week2_monday for i in issues)  # nobody at all in week 2


def test_hard_tag_rule_is_an_error():
    rule = TagRule(
        "Shift lead",
        TagRuleKind.MIN_ON_DUTY,
        hard=True,
        weekdays=frozenset({MON}),
        interval=Interval(540, 600),
    )
    r = validate(ctx([], rules=Rules(coverage=CoverageRules(default=0), tag_rules=[rule])))
    assert {i.severity for i in only(r, Category.TAG_RULE)} == {Severity.ERROR}


def test_tag_rule_met_when_tagged_person_on_duty():
    rule = TagRule(
        "Shift lead", TagRuleKind.MIN_ON_DUTY, weekdays=frozenset({MON}), interval=Interval(540, 600)
    )
    people = {1: person(1, tags={"Shift lead"})}
    shifts = [shift(1, MONDAY, 540, 660), shift(1, MONDAY + timedelta(days=7), 540, 660)]
    rules = Rules(coverage=CoverageRules(default=0), tag_rules=[rule])
    assert only(validate(ctx(shifts, people, rules, weeks=2)), Category.TAG_RULE) == []


def test_min_weekly_hours_tag_rule():
    rule = TagRule("Processor", TagRuleKind.MIN_WEEKLY_HOURS, minutes=15 * 60)
    people = {1: person(1, tags={"Processor"})}
    rules = Rules(coverage=CoverageRules(default=0), tag_rules=[rule])
    shifts = week_of_shifts(1, 0, 3) + week_of_shifts(1, 1, 2)
    issues = only(validate(ctx(shifts, people, rules, weeks=2)), Category.TAG_RULE)
    assert [i.minutes for i in issues] == [300]  # week 2 has 10h


def test_not_alone_tag_rule():
    rule = TagRule("New employee", TagRuleKind.NOT_ALONE)
    people = {1: person(1, tags={"New employee"}), 2: person(2)}
    rules = Rules(coverage=CoverageRules(default=0), tag_rules=[rule])
    alone = only(
        validate(ctx([shift(1, MONDAY, 540, 720), shift(2, MONDAY, 600, 720)], people, rules)),
        Category.TAG_RULE,
    )
    assert [i.interval for i in alone] == [Interval(540, 600)]
    paired = only(
        validate(ctx([shift(1, MONDAY, 540, 720), shift(2, MONDAY, 540, 720)], people, rules)),
        Category.TAG_RULE,
    )
    assert paired == []


def test_not_alone_strict_reading_counts_two_new_employees_as_alone():
    rule = TagRule("New employee", TagRuleKind.NOT_ALONE)
    people = {1: person(1, tags={"New employee"}), 2: person(2, tags={"New employee"})}
    rules = Rules(coverage=CoverageRules(default=0), tag_rules=[rule])
    together = [shift(1, MONDAY, 540, 720), shift(2, MONDAY, 540, 720)]
    issues = only(validate(ctx(together, people, rules)), Category.TAG_RULE)
    assert [i.interval for i in issues] == [Interval(540, 720)]
    assert issues[0].message.startswith("A New employee always has someone without the tag on duty")


def test_not_solo_tag_rule_lets_new_employees_work_together():
    rule = TagRule("New employee", TagRuleKind.NOT_SOLO)
    people = {1: person(1, tags={"New employee"}), 2: person(2, tags={"New employee"})}
    rules = Rules(coverage=CoverageRules(default=0), tag_rules=[rule])
    together = [shift(1, MONDAY, 540, 720), shift(2, MONDAY, 600, 720)]
    issues = only(validate(ctx(together, people, rules)), Category.TAG_RULE)
    assert [i.interval for i in issues] == [Interval(540, 600)]  # alone only before 10am
    assert issues[0].message == "A New employee never works alone: alone on duty Mon 18 Jan 9am–10am"
    paired = [shift(1, MONDAY, 540, 720), shift(2, MONDAY, 540, 720)]
    assert only(validate(ctx(paired, people, rules)), Category.TAG_RULE) == []


def test_time_bound_tag_only_counts_on_its_days():
    rule = TagRule(
        "Shift lead", TagRuleKind.MIN_ON_DUTY, weekdays=frozenset({MON}), interval=Interval(540, 600)
    )
    people = {1: person(1, tags={"Shift lead"}, tag_days=[MONDAY])}  # lead for week 1 only
    shifts = [shift(1, MONDAY, 540, 660), shift(1, MONDAY + timedelta(days=7), 540, 660)]
    rules = Rules(coverage=CoverageRules(default=0), tag_rules=[rule])
    issues = only(validate(ctx(shifts, people, rules, weeks=2)), Category.TAG_RULE)
    assert [i.day for i in issues] == [MONDAY + timedelta(days=7)]


# --- Drift and summaries -------------------------------------------------------------------


def test_drift_against_published_version():
    published = [shift(1, MONDAY, 540, 660, sid=10), shift(2, MONDAY, 660, 780, sid=11)]
    current = [shift(1, MONDAY, 540, 690, sid=10), shift(3, MONDAY, 780, 900, sid=12)]
    drift = only(validate(ctx(current, rules=single_day_rules(), published=published)), Category.DRIFT)
    kinds = sorted(i.message.split(":")[0] for i in drift)
    assert kinds == ["Changed", "New", "Removed"]
    assert all(i.severity == Severity.INFO for i in drift)


def test_open_shift_is_info():
    r = validate(ctx([Shift(None, None, MONDAY, Interval(540, 780))], people={}, rules=single_day_rules()))
    assert [i.category for i in r.info] == [Category.OPEN_SHIFT]
    assert only(r, Category.COVERAGE_GAP)  # an open shift doesn't cover anything


def test_summary_counts_by_category():
    r = validate(ctx([shift(1, MONDAY, 540, 600)], rules=single_day_rules()))
    assert r.summary() == "2 issues: 1 coverage gap, 1 shift shape issue"


# --- Duties --------------------------------------------------------------------------------


def test_opener_duty_missing_and_met():
    from domain.rules import OpenerDuty

    rules = Rules(
        coverage=CoverageRules(default=0), openers=[OpenerDuty(1, frozenset({MON}), frozenset({MON}), 90)]
    )
    people = {1: person(1)}
    missing = only(validate(ctx([shift(1, MONDAY, 480, 600)], people, rules)), Category.DUTY)
    assert len(missing) == 1 and "no opening shift" in missing[0].message
    met = only(validate(ctx([shift(1, MONDAY, 450, 540)], people, rules)), Category.DUTY)
    assert met == []
    assert (
        only(validate(ctx([shift(1, MONDAY, 450, 540)], people, rules)), Category.SHAPE) == []
    )  # 1.5h allowed


def test_lead_week_duty():
    from domain.rules import WeekHoursDuty

    rules = Rules(
        coverage=CoverageRules(default=0), week_duties=[WeekHoursDuty(1, frozenset({0}), 20 * 60, 10 * 60)]
    )
    people = {1: person(1)}
    shifts = week_of_shifts(1, 0, 4) + week_of_shifts(1, 1, 3)  # 20h in the lead week, 15h after
    issues = only(validate(ctx(shifts, people, rules, weeks=2)), Category.DUTY)
    assert [(i.severity, i.minutes) for i in issues] == [(Severity.ERROR, 300)]
    short = only(validate(ctx(week_of_shifts(1, 0, 3), people, rules, weeks=2)), Category.DUTY)
    assert [(i.severity, i.minutes) for i in short] == [(Severity.WARNING, 300)]


# --- Shifts on other schedules (D95) --------------------------------------------------------


def test_overlap_with_a_shift_on_another_schedule_is_double_booking():
    own = shift(1, MONDAY, 540, 720, sid=1)
    other = shift(1, MONDAY, 660, 840, sid=99)
    c = replace(ctx([own], rules=single_day_rules()), elsewhere=[other])
    issues = [i for i in validate(c).issues if i.category == Category.DOUBLE_BOOKING]
    assert len(issues) == 1 and "on another schedule" in issues[0].message
    assert issues[0].shift_id == 1  # points at the shift on this schedule


def test_weekly_caps_count_hours_on_other_schedules():
    rules = Rules(coverage=CoverageRules(default=0), caps=[HourCap(CapScope.WEEK, 10 * 60)])
    own = shift(1, MONDAY, 540, 900, sid=1)  # 6h here
    other = shift(1, MONDAY + timedelta(days=1), 540, 900, sid=99)  # 6h elsewhere, same week
    assert not [i for i in validate(ctx([own], rules=rules)).issues if i.category == Category.CAP]
    caps = [
        i
        for i in validate(replace(ctx([own], rules=rules), elsewhere=[other])).issues
        if i.category == Category.CAP
    ]
    assert len(caps) == 1 and "12" in caps[0].message


def test_problems_only_on_other_schedules_are_left_to_them():
    rules = Rules(coverage=CoverageRules(default=0), caps=[HourCap(CapScope.WEEK, 2 * 60)])
    away = [shift(2, MONDAY, 540, 780, sid=98), shift(2, MONDAY, 600, 840, sid=99)]  # overlap each other
    own = [shift(1, MONDAY, 540, 720, sid=1), shift(2, MONDAY + timedelta(days=1), 540, 660, sid=2)]
    c = replace(ctx(own, rules=rules), elsewhere=away)
    mine = [i for i in validate(c).issues if i.employee_id == 2]
    assert not [i for i in mine if i.category == Category.DOUBLE_BOOKING]  # theirs to report
    assert [i for i in mine if i.category == Category.CAP]  # but the hours still count here
