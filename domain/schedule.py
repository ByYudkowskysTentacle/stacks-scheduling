# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shifts and the context the validation suite and the engine work in (SPEC §7.1)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

from domain.availability import WeeklyAvailability
from domain.calendar import Closure, OperatingHours, open_intervals, week_start_for
from domain.rules import Rules
from domain.timeutil import SLOT_MINUTES, Interval, merge


@dataclass(frozen=True)
class Shift:
    """One assignment. `employee_id` None is an open shift (SPEC §2)."""

    id: int | None
    employee_id: int | None
    day: date
    interval: Interval
    pay_role_id: int | None = None
    location_id: int | None = None
    locked: bool = False

    @property
    def minutes(self) -> int:
        return self.interval.minutes

    def same_assignment(self, other: Shift) -> bool:
        return (self.employee_id, self.day, self.interval, self.pay_role_id, self.location_id) == (
            other.employee_id,
            other.day,
            other.interval,
            other.pay_role_id,
            other.location_id,
        )


@dataclass(frozen=True)
class Person:
    """What the rules need to know about an employee. Tags are per day because they can be
    time-bound (SPEC §4.3)."""

    id: int
    name: str
    availability: WeeklyAvailability
    tags_by_day: Mapping[date, frozenset[str]] = field(default_factory=dict[date, frozenset[str]])
    auto_schedulable: bool = True  # False for non_regular and inactive (SPEC §4.1)

    def has_tag(self, tag: str, day: date) -> bool:
        return tag in self.tags_by_day.get(day, frozenset())

    def tagged_in(self, tag: str, days: Iterable[date]) -> bool:
        return any(self.has_tag(tag, d) for d in days)


@dataclass(frozen=True)
class Period:
    start: date
    weeks: int
    week_start: int

    def __post_init__(self) -> None:
        if self.start.weekday() != self.week_start:
            raise ValueError("a planning period starts on the week start day")

    @property
    def days(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range(self.weeks * 7)]

    @property
    def end(self) -> date:
        return self.start + timedelta(days=self.weeks * 7 - 1)

    def week_of(self, day: date) -> int:
        return (day - self.start).days // 7

    def week_days(self, week: int) -> list[date]:
        return self.days[week * 7 : (week + 1) * 7]

    def week_label(self, week: int) -> str:
        return f"week {week + 1} ({self.week_days(week)[0]:%a %-d %b})"


@dataclass(frozen=True)
class ScheduleContext:
    period: Period
    hours: Sequence[OperatingHours]
    closures: Sequence[Closure]
    people: Mapping[int, Person]
    rules: Rules
    shifts: Sequence[Shift]
    published: Sequence[Shift] | None = None  # the last published version, for drift
    term_minutes_outside_period: Mapping[int, int] = field(default_factory=dict[int, int])

    def open_on(self, day: date) -> list[Interval]:
        return open_intervals(day, self.hours, self.closures)

    def assigned(self) -> list[Shift]:
        return [s for s in self.shifts if s.employee_id is not None]


def slots_of(interval: Interval) -> range:
    return range(interval.start // SLOT_MINUTES, -(-interval.end // SLOT_MINUTES))


def on_duty_by_slot(shifts: Iterable[Shift]) -> dict[date, dict[int, list[int]]]:
    """Employee ids on duty per day and slot index. Open shifts don't count as coverage."""
    out: dict[date, dict[int, list[int]]] = defaultdict(lambda: defaultdict(list))
    for s in shifts:
        if s.employee_id is None:
            continue
        for slot in slots_of(s.interval):
            out[s.day][slot].append(s.employee_id)
    return out


def minutes_by_person_week(shifts: Iterable[Shift], period: Period) -> dict[int, dict[int, int]]:
    out: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for s in shifts:
        if s.employee_id is not None and period.start <= s.day <= period.end:
            out[s.employee_id][period.week_of(s.day)] += s.minutes
    return out


def runs(slots: Iterable[int]) -> list[Interval]:
    """Consecutive slot indexes as minute intervals."""
    return merge(Interval(s * SLOT_MINUTES, (s + 1) * SLOT_MINUTES) for s in slots)


def week_start_of(day: date, period: Period) -> date:
    return week_start_for(day, period.week_start)
