# SPDX-License-Identifier: AGPL-3.0-or-later
from datetime import date

import pytest

from domain.calendar import (
    FRI,
    MON,
    SAT,
    SUN,
    THU,
    TUE,
    WED,
    Closure,
    OperatingHours,
    open_intervals,
    open_minutes_in_week,
    week_start_for,
)
from domain.timeutil import Interval, format_minute_12h, parse_minute, subtract

PILOT = OperatingHours(
    effective_from=date(2027, 1, 1),
    effective_to=None,
    intervals={
        **{wd: [Interval(450, 1320)] for wd in (MON, TUE, WED, THU)},
        FRI: [Interval(450, 960)],
        SAT: [Interval(720, 960)],
        SUN: [Interval(1080, 1320)],
    },
)
CHAPEL = Closure(reason="Chapel", weekday=WED, interval=Interval(600, 660))
SATURDAY = date(2027, 1, 16)


def test_pilot_weekly_open_hours_without_closure():
    assert open_minutes_in_week(SATURDAY, [PILOT], []) == 74.5 * 60


def test_chapel_closure_takes_an_hour_off_the_week():
    # SPEC §3.1: the pilot's weekly open hours went from 74.5 to 73.5 when Chapel was added.
    assert open_minutes_in_week(SATURDAY, [PILOT], [CHAPEL]) == 73.5 * 60


def test_closure_splits_wednesday():
    wednesday = date(2027, 1, 20)
    assert open_intervals(wednesday, [PILOT], [CHAPEL]) == [Interval(450, 600), Interval(660, 1320)]


def test_dated_all_day_closure():
    holiday = Closure(reason="Holiday", on=date(2027, 1, 18))
    assert open_intervals(date(2027, 1, 18), [PILOT], [holiday]) == []
    assert open_intervals(date(2027, 1, 25), [PILOT], [holiday]) == [Interval(450, 1320)]


def test_recurring_closure_respects_its_date_range():
    chapel_from_feb = Closure(reason="Chapel", weekday=WED, interval=Interval(600, 660), valid_from=date(2027, 2, 1))
    assert len(open_intervals(date(2027, 1, 20), [PILOT], [chapel_from_feb])) == 1
    assert len(open_intervals(date(2027, 2, 3), [PILOT], [chapel_from_feb])) == 2


def test_later_operating_hours_override_earlier_ones():
    finals = OperatingHours(date(2027, 5, 1), date(2027, 5, 7), {wd: [Interval(420, 1440)] for wd in range(7)})
    assert open_intervals(date(2027, 5, 3), [PILOT, finals], []) == [Interval(420, 1440)]
    assert open_intervals(date(2027, 5, 10), [PILOT, finals], []) == [Interval(450, 1320)]


def test_no_hours_means_closed():
    assert open_intervals(date(2026, 12, 1), [PILOT], []) == []


def test_closure_must_be_recurring_or_dated():
    with pytest.raises(ValueError):
        Closure(reason="x")
    with pytest.raises(ValueError):
        Closure(reason="x", weekday=MON, on=date(2027, 1, 1))


def test_saturday_week_start():
    assert week_start_for(date(2027, 1, 20), SAT) == SATURDAY  # a Wednesday
    assert week_start_for(SATURDAY, SAT) == SATURDAY
    assert week_start_for(date(2027, 1, 22), SAT) == SATURDAY  # Friday ends the week


def test_time_parsing_and_formatting():
    assert parse_minute("07:30") == 450
    assert parse_minute("24:00") == 1440
    for bad in ("7.30", "25:00", "10:60", "", "10:5"):
        with pytest.raises(ValueError):
            parse_minute(bad)
    assert format_minute_12h(450) == "7:30am"
    assert format_minute_12h(720) == "12pm"
    assert format_minute_12h(1320) == "10pm"


def test_subtract_and_invalid_interval():
    assert subtract([Interval(0, 100)], [Interval(10, 20), Interval(50, 200)]) == [Interval(0, 10), Interval(20, 50)]
    with pytest.raises(ValueError):
        Interval(10, 10)
