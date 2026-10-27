# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shifts and the context the validation suite and the engine work in (SPEC §7.1)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

from domain.availability import WeeklyAvailability
from domain.calendar import Closure, OperatingHours, open_intervals, week_days, week_start_for
from domain.rules import Rules
from domain.timeutil import SLOT_MINUTES, Interval, merge, total_minutes


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
    # Approved time off (SPEC §5.1 date-specific exceptions): whole days are (0, 1440).
    time_off: Mapping[date, tuple[Interval, ...]] = field(default_factory=dict[date, tuple[Interval, ...]])

    def has_tag(self, tag: str, day: date) -> bool:
        return tag in self.tags_by_day.get(day, frozenset())

    def tagged_in(self, tag: str, days: Iterable[date]) -> bool:
        return any(self.has_tag(tag, d) for d in days)

    def off_on(self, day: date) -> list[Interval]:
        """Approved time off on `day`, widened to whole slots."""
        return merge(
            Interval(iv.start // SLOT_MINUTES * SLOT_MINUTES, -(-iv.end // SLOT_MINUTES) * SLOT_MINUTES)
            for iv in self.time_off.get(day, ())
        )

    def blocked_on(self, day: date) -> list[Interval]:
        """Everything that rules the person out on `day`: weekly unavailability and time off."""
        return merge([*self.availability.unavailable_for_scheduling(day.weekday()), *self.off_on(day)])


@dataclass(frozen=True)
class Period:
    """A planning period: every day from `start` to `end`, inclusive. That can be whole weeks,
    calendar months or a whole term (D120).

    Weeks are calendar weeks counted from the week start, and week 0 is the one containing
    `start`. A period that doesn't start on a week start, or doesn't end the day before one, has
    part weeks at its edges: `week_days` gives the days inside the period, `calendar_week` all
    seven.
    """

    start: date
    end: date
    week_start: int

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError("a planning period can't end before it starts")

    @classmethod
    def of_weeks(cls, start: date, weeks: int, week_start: int) -> Period:
        """Whole weeks from a week start (SPEC §3.2)."""
        if start.weekday() != week_start:
            raise ValueError("a period of whole weeks starts on the week start day")
        return cls(start, start + timedelta(days=weeks * 7 - 1), week_start)

    @property
    def days(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range((self.end - self.start).days + 1)]

    @property
    def first_week_start(self) -> date:
        return week_start_for(self.start, self.week_start)

    @property
    def weeks(self) -> int:
        """The calendar weeks the period touches, part weeks included."""
        return self.week_of(self.end) + 1

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end

    def week_of(self, day: date) -> int:
        return (week_start_for(day, self.week_start) - self.first_week_start).days // 7

    def calendar_week(self, week: int) -> list[date]:
        """All seven days of week `week`, including any outside the period."""
        return week_days(self.first_week_start + timedelta(days=7 * week))

    def week_days(self, week: int) -> list[date]:
        """The days of week `week` inside the period."""
        return [d for d in self.calendar_week(week) if self.contains(d)]

    def is_part_week(self, week: int) -> bool:
        return len(self.week_days(week)) < 7

    def in_weeks(self, day: date) -> bool:
        """Whether `day` is in one of the period's calendar weeks, inside the period or not."""
        return self.first_week_start <= day <= self.calendar_week(self.weeks - 1)[-1]

    def week_label(self, week: int) -> str:
        days = self.week_days(week)
        if len(days) == 7:
            return f"week {week + 1} ({days[0]:%a %-d %b})"
        return f"week {week + 1} ({days[0]:%a %-d %b} to {days[-1]:%a %-d %b}, part week)"


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
    # These people's shifts on other schedules (another location or department) during this
    # period. They count toward weekly and period caps, daily limits and double booking, never
    # toward coverage, and nothing here changes them (D95).
    elsewhere: Sequence[Shift] = ()

    def open_on(self, day: date) -> list[Interval]:
        return open_intervals(day, self.hours, self.closures)

    def elsewhere_on(self, employee_id: int, day: date) -> list[Shift]:
        return [s for s in self.elsewhere if s.employee_id == employee_id and s.day == day]

    def weekly_goal(self, minutes: int, week: int) -> int:
        """A weekly goal (an hour target, desired hours, weekly tag hours or a lead week's hours)
        for week `week`. In a part week it's scaled by the week's share of the calendar week's
        open hours, rounded down to a slot, so a period's edge never asks for a whole week's
        hours in a few days (D120). Caps are never scaled: they count the whole calendar week."""
        if not self.period.is_part_week(week):
            return minutes
        whole = sum(total_minutes(self.open_on(d)) for d in self.period.calendar_week(week))
        part = sum(total_minutes(self.open_on(d)) for d in self.period.week_days(week))
        if part == whole:  # the rest of the week is closed, so this part is the whole week
            return minutes
        return minutes * part // whole // SLOT_MINUTES * SLOT_MINUTES

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
    """Minutes per person and period week. Shifts on the days of a part week outside the period
    (on another schedule) count toward that week, as weekly caps need (D120)."""
    out: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for s in shifts:
        if s.employee_id is not None and period.in_weeks(s.day):
            out[s.employee_id][period.week_of(s.day)] += s.minutes
    return out


def minutes_in_period(shifts: Iterable[Shift], period: Period) -> dict[int, int]:
    """Minutes per person on the period's own days, for period caps."""
    out: dict[int, int] = defaultdict(int)
    for s in shifts:
        if s.employee_id is not None and period.contains(s.day):
            out[s.employee_id] += s.minutes
    return out


def runs(slots: Iterable[int]) -> list[Interval]:
    """Consecutive slot indexes as minute intervals."""
    return merge(Interval(s * SLOT_MINUTES, (s + 1) * SLOT_MINUTES) for s in slots)


def week_start_of(day: date, period: Period) -> date:
    return week_start_for(day, period.week_start)
