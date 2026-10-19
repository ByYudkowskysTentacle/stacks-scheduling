# SPDX-License-Identifier: AGPL-3.0-or-later
"""Suggestion queries (SPEC §7.5, scenario 13) and remove-from-pool (scenario 9)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

from domain.availability import AvailabilityBlock, AvailabilityStatus, BlockKind, WeeklyAvailability
from domain.calendar import MON, SAT, WED
from domain.rules import CapScope, HourCap, HourTarget, OpenerDuty, Rotation, RotationMode, Rules
from domain.schedule import Period, ScheduleContext
from domain.suggest import gap_shift, opportunities, remove_from_pool_plan, who_could_take, widen_gap
from domain.timeutil import Interval
from domain.validation import Category, validate
from tests.domain.test_calendar import CHAPEL, PILOT
from tests.domain.test_validation import MONDAY, START, person, shift, single_day_rules

NEXT_MONDAY = MONDAY + timedelta(days=7)


def unavailable(weekday, start, end):
    return WeeklyAvailability(
        AvailabilityStatus.ENTERED, (AvailabilityBlock(weekday, BlockKind.UNAVAILABLE, Interval(start, end)),)
    )


def preferring(weekday, start, end):
    return WeeklyAvailability(
        AvailabilityStatus.ENTERED, (AvailabilityBlock(weekday, BlockKind.PREFERRED, Interval(start, end)),)
    )


def context(shifts, people, rules=None, weeks=1):
    return ScheduleContext(Period(START, weeks, SAT), [PILOT], [CHAPEL], people, rules or Rules(), shifts)


def ids(takers):
    return [t.employee_id for t in takers]


# --- Who could take this shift? -------------------------------------------------------------


def test_eligible_people_ranked_by_preferred_time_then_hours():
    target = shift(1, MONDAY, 540, 660, sid=1)
    busy = shift(3, date(2027, 1, 19), 540, 780, sid=2)
    people = {
        1: person(1),
        2: person(2),
        3: person(3),
        4: person(4, availability=preferring(MON, 480, 720)),
    }
    result = who_could_take(context([target, busy], people), target)
    assert ids(result.eligible) == [4, 2, 3]  # preferred first, then fewest hours
    assert result.eligible[0].preferred
    assert result.eligible[2].week_minutes == 240


def test_blocked_people_say_which_rule_stops_them():
    target = shift(1, MONDAY, 540, 660, sid=1)
    people = {
        1: person(1),
        2: person(2, availability=unavailable(MON, 600, 650)),
        3: person(3),
        4: person(4),
        5: person(5),
    }
    other = shift(3, MONDAY, 600, 720, sid=2)
    rules = Rules(
        caps=[HourCap(CapScope.WEEK, 60, employee_id=4)],
        rotation=Rotation(RotationMode.STRICT, {5: "B"}),
    )
    result = who_could_take(context([target, other], people, rules), target)
    why = {t.employee_id: " ".join(t.blockers) for t in result.blocked}
    assert "unavailable Mon 18 Jan 10am–10:50am" in why[2]
    assert "P3" in why[3]  # already working then: double booking
    assert "cap" in why[4].lower() or "1h" in why[4]
    assert "rotation group B" in why[5]
    assert result.eligible == []


def test_not_in_pool_is_blocked():
    target = shift(1, MONDAY, 540, 660, sid=1)
    staff = replace(person(2), auto_schedulable=False)
    result = who_could_take(context([target], {1: person(1), 2: staff}), target)
    assert result.blocked[0].blockers == ("P2 isn't in the scheduling pool",)


def test_not_entered_availability_is_a_warning_and_ranks_last():
    target = shift(1, MONDAY, 540, 660, sid=1)
    people = {
        1: person(1),
        2: person(2, availability=WeeklyAvailability(AvailabilityStatus.NOT_ENTERED)),
        3: person(3),
    }
    result = who_could_take(
        context([target, shift(3, NEXT_MONDAY, 540, 780, sid=2)], people, weeks=2), target
    )
    assert ids(result.eligible) == [3, 2]
    assert "hasn't entered availability" in result.eligible[1].warnings[0]


def test_giving_away_an_opening_shift_is_flagged_as_protected():
    opening = shift(1, MONDAY, 450, 570, sid=1)
    rules = Rules(openers=[OpenerDuty(1, frozenset({MON}))])
    result = who_could_take(context([opening], {1: person(1), 2: person(2)}, rules), opening)
    assert result.holder_effects == ("P1 has no opening shift on Mon 18 Jan",)
    assert ids(result.eligible) == [2]  # the duty is the holder's, not a reason to block P2


def test_who_could_cover_a_gap():
    gap = gap_shift(MONDAY, Interval(540, 660))
    people = {1: person(1), 2: person(2, availability=unavailable(MON, 540, 600))}
    result = who_could_take(context([], people, single_day_rules()), gap)
    assert ids(result.eligible) == [1]
    assert result.holder_effects == ()


def test_short_gap_is_widened_to_the_minimum_shift():
    ctx = context([], {1: person(1)})
    assert widen_gap(ctx, MONDAY, Interval(1260, 1290)) == Interval(1200, 1320)  # closes at 10pm
    assert widen_gap(ctx, MONDAY, Interval(540, 570)) == Interval(540, 660)
    assert widen_gap(ctx, MONDAY, Interval(540, 720)) == Interval(540, 720)


# --- How can I raise this person's hours? (scenario 13) -------------------------------------


def test_opportunities_grouped_by_week_with_holder_hours():
    shifts = [
        shift(2, MONDAY, 540, 780, sid=1),
        shift(2, date(2027, 1, 19), 540, 780, sid=2),
        shift(3, NEXT_MONDAY, 540, 660, sid=3),
        shift(4, MONDAY, 1080, 1320, sid=4),  # E can't: class Monday evening
    ]
    people = {
        1: person(1, availability=unavailable(MON, 1080, 1320)),
        2: person(2),
        3: person(3),
        4: person(4),
    }
    result = opportunities(context(shifts, people, weeks=2), 1)
    assert {w: [o.shift.id for o in items] for w, items in result.by_week.items()} == {0: [1, 2], 1: [3]}
    first = result.by_week[0][0]
    assert first.holder_name == "P2" and first.holder_week_minutes == 480
    assert not first.holder_is_low
    assert result.by_week[1][0].holder_is_low  # P3 has only 2h that week


def test_opportunities_exclude_shifts_protected_by_duties_or_targets():
    shifts = [
        shift(2, MONDAY, 450, 570, sid=1),  # P2's opening duty
        shift(3, MONDAY, 600, 720, sid=2),  # P3 needs 2h for their target
        shift(4, MONDAY, 780, 900, sid=3),  # free to take
    ]
    rules = Rules(openers=[OpenerDuty(2, frozenset({MON}))], targets=[HourTarget(3, 120)])
    people = {p: person(p) for p in (1, 2, 3, 4)}
    result = opportunities(context(shifts, people, rules), 1)
    assert [o.shift.id for o in result.by_week[0]] == [3]
    assert result.protected == 2


# --- Remove from pool (scenario 9) ----------------------------------------------------------


def test_remove_from_pool_reassigns_at_the_same_times_without_deleting():
    w = 9
    shifts = [
        shift(w, MONDAY, 540, 780, sid=1),
        shift(w, date(2027, 1, 19), 540, 780, sid=2),
        shift(w, date(2027, 1, 22), 600, 840, sid=3),
        shift(2, date(2027, 1, 19), 1080, 1320, sid=4),
    ]
    people = {w: person(w), 2: person(2), 3: person(3), 4: person(4, availability=unavailable(MON, 0, 1440))}
    rules = Rules(caps=[HourCap(CapScope.WEEK, 240, employee_id=3)])
    ctx = context(shifts, people, rules)
    plan = remove_from_pool_plan(ctx, w, START)
    assert [(m.shift.id, m.shift.day, m.shift.interval) for m in plan] == [
        (s.id, s.day, s.interval) for s in shifts[:3]
    ]
    assert all(m.to_id not in (None, w) for m in plan)
    assert plan[0].to_id != 4  # unavailable on Mondays
    # P3's 4h cap means they take at most one shift.
    assert sum(m.to_id == 3 for m in plan) <= 1
    moved = {m.shift.id: m.to_id for m in plan}
    after = [replace(s, employee_id=moved.get(s.id, s.employee_id)) for s in shifts]
    before_gaps = [i for i in validate(ctx).issues if i.category == Category.COVERAGE_GAP]
    after_gaps = [
        i for i in validate(context(after, people, rules)).issues if i.category == Category.COVERAGE_GAP
    ]
    assert after_gaps == before_gaps  # coverage stays intact


def test_remove_from_pool_leaves_an_open_shift_when_nobody_can_take_it():
    shifts = [shift(1, MONDAY, 540, 780, sid=1)]
    people = {1: person(1), 2: person(2, availability=unavailable(MON, 540, 600))}
    plan = remove_from_pool_plan(context(shifts, people), 1, START)
    assert [(m.to_id, m.to_name) for m in plan] == [(None, "Open shift")]


def test_remove_from_pool_keeps_past_shifts():
    shifts = [shift(1, MONDAY, 540, 780, sid=1), shift(1, date(2027, 1, 21), 540, 780, sid=2)]
    plan = remove_from_pool_plan(context(shifts, {1: person(1), 2: person(2)}), 1, date(2027, 1, 20))
    assert [m.shift.id for m in plan] == [2]


def test_closures_block_every_candidate():
    wednesday = date(2027, 1, 20)
    assert wednesday.weekday() == WED
    result = who_could_take(context([], {1: person(1)}), gap_shift(wednesday, Interval(540, 720)))
    assert result.eligible == []
    assert "the library is closed Wed 20 Jan 10am–11am" in result.blocked[0].blockers
