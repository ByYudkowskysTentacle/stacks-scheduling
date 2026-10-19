# SPDX-License-Identifier: AGPL-3.0-or-later
"""Approved time off (SPEC §5.1, §8.2): a date-specific block that validation, the engine and
suggestions all respect."""

from __future__ import annotations

from dataclasses import replace

from domain.engine.candidates import Scope, build_candidates
from domain.suggest import who_could_take
from domain.timeutil import Interval
from domain.validation import Category, Severity, validate
from tests.domain.test_validation import MONDAY, ctx, person, shift, single_day_rules

OFF = {MONDAY: (Interval(540, 780),)}


def test_shift_during_approved_time_off_is_an_availability_error():
    p = replace(person(1), time_off=OFF)
    report = validate(ctx([shift(1, MONDAY, 600, 720)], people={1: p}, rules=single_day_rules()))
    clash = [i for i in report.issues if i.category == Category.AVAILABILITY]
    assert len(clash) == 1 and clash[0].severity == Severity.ERROR
    assert clash[0].message == "P1 has approved time off Mon 18 Jan 10am–12pm (shift 10am–12pm)"


def test_shift_outside_time_off_passes():
    p = replace(person(1), time_off=OFF)
    report = validate(ctx([shift(1, MONDAY, 780, 900)], people={1: p}, rules=single_day_rules()))
    assert not [i for i in report.issues if i.category == Category.AVAILABILITY]


def test_whole_day_off_partial_minutes_widen_to_slots():
    p = replace(person(1), time_off={MONDAY: (Interval(545, 600),)})
    assert p.off_on(MONDAY) == [Interval(540, 600)]
    assert replace(person(1), time_off={MONDAY: (Interval(0, 1440),)}).blocked_on(MONDAY) == [
        Interval(0, 1440)
    ]


def test_engine_never_offers_time_off_and_drops_clashing_shifts():
    p = replace(person(1), time_off=OFF)
    c = ctx([], people={1: p})
    cands = build_candidates(c, Scope()).candidates
    assert cands and not any(
        x.day == MONDAY and Interval(x.start, x.end).overlaps(Interval(540, 780)) for x in cands
    )
    existing = [shift(1, MONDAY, 600, 720, sid=7)]
    assert [s.id for s in build_candidates(c, Scope(), existing).dropped_existing] == [7]


def test_suggestions_skip_people_on_time_off():
    target = shift(2, MONDAY, 600, 720, sid=1)
    people = {1: replace(person(1), time_off=OFF), 2: person(2), 3: person(3)}
    result = who_could_take(ctx([target], people=people), target)
    assert [t.employee_id for t in result.eligible] == [3]
    assert "approved time off" in result.blocked[0].blockers[0]
