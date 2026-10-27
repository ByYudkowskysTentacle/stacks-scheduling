# SPDX-License-Identifier: AGPL-3.0-or-later
"""Planning periods that aren't whole weeks (D120): part weeks, weekly caps over the whole
calendar week, period caps over the period's days, and weekly goals scaled in part weeks. One
failing and one passing case per rule (CLAUDE.md §5)."""

from __future__ import annotations

from datetime import date

import pytest

from domain.availability import AvailabilityStatus, WeeklyAvailability
from domain.calendar import SAT, months_end
from domain.rules import CapScope, CoverageRules, HourCap, Rules, TagRule, TagRuleKind, WeekHoursDuty
from domain.schedule import Period, Person, ScheduleContext, Shift, minutes_by_person_week
from domain.timeutil import Interval
from domain.validation import Category, validate
from tests.domain.test_calendar import CHAPEL, PILOT

FEBRUARY = Period(date(2027, 2, 1), date(2027, 2, 28), SAT)  # Monday 1st to Sunday 28th
FREE = WeeklyAvailability(AvailabilityStatus.ENTERED)
NO_COVERAGE = CoverageRules(default=0)


def person(pid, tags=()):
    return Person(pid, f"P{pid}", FREE, {d: frozenset(tags) for d in FEBRUARY.days})


def shift(pid, day, start, end, sid=None):
    return Shift(sid, pid, day, Interval(start, end))


def ctx(shifts, rules, people=None, elsewhere=()):
    people = people or {1: person(1)}
    return ScheduleContext(FEBRUARY, [PILOT], [CHAPEL], people, rules, shifts, elsewhere=list(elsewhere))


def issues(report, category):
    return [i for i in report.issues if i.category == category]


# --- The period and its weeks ----------------------------------------------------------------


def test_a_month_has_part_weeks_at_both_edges():
    assert FEBRUARY.weeks == 5
    assert FEBRUARY.first_week_start == date(2027, 1, 30)
    assert FEBRUARY.week_days(0) == [date(2027, 2, d) for d in range(1, 6)]  # Mon to Fri
    assert len(FEBRUARY.calendar_week(0)) == 7
    assert [FEBRUARY.is_part_week(w) for w in range(5)] == [True, False, False, False, True]
    assert FEBRUARY.week_days(4) == [date(2027, 2, 27), date(2027, 2, 28)]
    assert FEBRUARY.week_label(0) == "week 1 (Mon 1 Feb to Fri 5 Feb, part week)"
    assert FEBRUARY.week_label(1) == "week 2 (Sat 6 Feb)"


def test_the_calendar_weeks_reach_past_the_period_but_no_further():
    assert FEBRUARY.in_weeks(date(2027, 1, 30)) and FEBRUARY.week_of(date(2027, 1, 30)) == 0
    assert FEBRUARY.in_weeks(date(2027, 3, 5)) and FEBRUARY.week_of(date(2027, 3, 5)) == 4
    assert not FEBRUARY.in_weeks(date(2027, 1, 29)) and not FEBRUARY.in_weeks(date(2027, 3, 6))
    assert not FEBRUARY.contains(date(2027, 1, 30))


def test_whole_weeks_still_start_on_the_week_start():
    assert Period.of_weeks(date(2027, 1, 16), 2, SAT).end == date(2027, 1, 29)
    with pytest.raises(ValueError, match="week start"):
        Period.of_weeks(date(2027, 1, 18), 2, SAT)


def test_a_period_cannot_end_before_it_starts():
    with pytest.raises(ValueError):
        Period(date(2027, 2, 2), date(2027, 2, 1), SAT)


def test_months_end_handles_year_ends_and_leap_years():
    assert months_end(date(2027, 2, 10), 1) == date(2027, 2, 28)
    assert months_end(date(2027, 11, 1), 3) == date(2028, 1, 31)
    assert months_end(date(2028, 2, 1), 1) == date(2028, 2, 29)


# --- Weekly goals in part weeks -------------------------------------------------------------


def test_weekly_goals_are_scaled_by_the_part_weeks_open_hours():
    c = ctx([], Rules())
    assert c.weekly_goal(1200, 1) == 1200  # a full week asks for the full goal
    # Mon-Fri hold 65.5 of the week's 73.5 open hours: 20h becomes 17.75h, rounded down to a slot.
    assert c.weekly_goal(1200, 0) == 1065
    assert c.weekly_goal(1200, 4) == 120  # Sat and Sun hold 8 of 73.5 hours


def test_a_part_week_whose_other_days_are_closed_keeps_the_whole_goal():
    from domain.calendar import OperatingHours

    weekdays_only = OperatingHours(date(2027, 1, 1), None, {wd: [Interval(540, 1020)] for wd in range(5)})
    c = ScheduleContext(FEBRUARY, [weekdays_only], [], {1: person(1)}, Rules(), [])
    assert c.weekly_goal(1200, 0) == 1200  # Mon-Fri is all the opening there is that week


# --- Weekly caps count the whole calendar week -----------------------------------------------

WEEK_CAP = Rules(coverage=NO_COVERAGE, caps=[HourCap(CapScope.WEEK, 20 * 60)])
OWN_16H = [shift(1, date(2027, 2, d), 600, 840, sid=d) for d in (1, 2, 3, 4)]  # Mon-Thu 10-2


def test_weekly_cap_in_a_part_week_counts_days_on_the_neighbouring_period():
    # The previous month's period has Sat 30 and Sun 31 Jan: 8h more in the same calendar week.
    before = [shift(1, date(2027, 1, 30), 720, 960, sid=90), shift(1, date(2027, 1, 31), 1080, 1320, sid=91)]
    caps = issues(validate(ctx(OWN_16H, WEEK_CAP, elsewhere=before)), Category.CAP)
    assert len(caps) == 1
    assert caps[0].minutes == 4 * 60
    assert (
        "24h in the week of Sat 30 Jan (the whole week, including days outside this period)"
        in caps[0].message
    )


def test_weekly_cap_in_a_part_week_passes_within_the_whole_week():
    before = [shift(1, date(2027, 1, 30), 720, 960, sid=90)]  # 20h over the calendar week
    earlier = [shift(1, date(2027, 1, 29), 600, 840, sid=92)]  # the week before: doesn't count
    assert issues(validate(ctx(OWN_16H, WEEK_CAP, elsewhere=before + earlier)), Category.CAP) == []
    assert minutes_by_person_week(before + earlier, FEBRUARY) == {1: {0: 240}}


# --- Period caps count the period's own days -------------------------------------------------

PERIOD_CAP = Rules(coverage=NO_COVERAGE, caps=[HourCap(CapScope.PERIOD, 14 * 60)])
OWN_12H = OWN_16H[:3]


def test_period_cap_counts_another_schedule_inside_the_period():
    other = [shift(1, date(2027, 2, 4), 600, 780, sid=93)]  # Thu 4 Feb, another department: 3h
    caps = issues(validate(ctx(OWN_12H, PERIOD_CAP, elsewhere=other)), Category.CAP)
    assert [c.minutes for c in caps] == [60]


def test_period_cap_leaves_out_days_outside_the_period():
    other = [shift(1, date(2027, 1, 30), 720, 960, sid=94)]  # in the calendar week, not the period
    assert issues(validate(ctx(OWN_12H, PERIOD_CAP, elsewhere=other)), Category.CAP) == []


# --- Weekly tag hours and lead-week hours are scaled in part weeks ---------------------------

PROCESSOR = Rules(
    coverage=NO_COVERAGE, tag_rules=[TagRule("Processor", TagRuleKind.MIN_WEEKLY_HOURS, minutes=1200)]
)


def _last_week(report):
    return [i for i in issues(report, Category.TAG_RULE) if "week 5" in i.message]


def test_weekly_tag_hours_in_a_part_week_ask_for_the_scaled_goal():
    people = {1: person(1, tags={"Processor"})}
    missed = _last_week(validate(ctx([], PROCESSOR, people)))
    assert len(missed) == 1 and missed[0].minutes == 120
    assert "2h for this part week" in missed[0].message


def test_weekly_tag_hours_in_a_part_week_met_by_the_scaled_goal():
    people = {1: person(1, tags={"Processor"})}
    saturday = [shift(1, date(2027, 2, 27), 720, 840, sid=1)]  # 2h on Sat 27 Feb
    assert _last_week(validate(ctx(saturday, PROCESSOR, people))) == []


LEAD = Rules(coverage=NO_COVERAGE, week_duties=[WeekHoursDuty(1, frozenset({0}), 20 * 60, 40 * 60)])


def _lead_week(report):
    return [i for i in issues(report, Category.DUTY) if "week 1" in i.message]


def test_lead_week_in_a_part_week_short_of_the_scaled_hours():
    missed = _lead_week(validate(ctx(OWN_16H, LEAD)))
    assert len(missed) == 1 and "17.75h expected" in missed[0].message


def test_lead_week_in_a_part_week_meets_the_scaled_hours():
    friday = [shift(1, date(2027, 2, 5), 600, 720, sid=5)]  # 16h + 2h = 18h of 17.75h
    assert _lead_week(validate(ctx(OWN_16H + friday, LEAD))) == []
