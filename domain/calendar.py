# SPDX-License-Identifier: AGPL-3.0-or-later
"""Operating hours, closures and weeks (SPEC §3.1, §3.2).

Weekdays use Python's numbering (Monday 0 to Sunday 6). The week start is
configuration; the pilot's weeks run Saturday to Friday (D27).
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

from domain.timeutil import Interval, merge, subtract, total_minutes

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
WEEKDAY_SHORT = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
ALL_DAY = Interval(0, 24 * 60)


def week_start_for(day: date, week_start: int) -> date:
    """The first day of the week containing `day`."""
    return day - timedelta(days=(day.weekday() - week_start) % 7)


def week_days(start: date) -> list[date]:
    return [start + timedelta(days=i) for i in range(7)]


def month_start(day: date) -> date:
    return day.replace(day=1)


def months_end(start: date, months: int) -> date:
    """The last day of the `months`-th calendar month counted from the one containing `start`."""
    index = start.year * 12 + start.month - 1 + months - 1
    year, month = divmod(index, 12)
    return date(year, month + 1, calendar.monthrange(year, month + 1)[1])


def ordered_weekdays(week_start: int) -> list[int]:
    """Weekdays in display order for a given week start (Saturday first for the pilot)."""
    return [(week_start + i) % 7 for i in range(7)]


@dataclass(frozen=True)
class OperatingHours:
    """Open intervals per weekday, effective over a date range (finals week, summer).

    `effective_to` is inclusive; None means open-ended. When several sets cover a
    date, the one with the latest `effective_from` wins, so a short override such
    as finals week sits on top of the term's regular hours.
    """

    effective_from: date
    effective_to: date | None
    intervals: Mapping[int, Sequence[Interval]] = field(default_factory=dict[int, Sequence[Interval]])

    def covers(self, day: date) -> bool:
        return self.effective_from <= day and (self.effective_to is None or day <= self.effective_to)


@dataclass(frozen=True)
class Closure:
    """A time a location is closed inside operating hours (SPEC §3.1).

    Recurring closures set `weekday` and an optional date range; dated closures set
    `on`. `interval` None means all day. `reason` is shown in the UI ("Chapel").
    """

    reason: str
    interval: Interval | None = None
    weekday: int | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    on: date | None = None

    def __post_init__(self) -> None:
        if (self.weekday is None) == (self.on is None):
            raise ValueError("a closure is either recurring (weekday) or dated (on), not both")

    @property
    def recurring(self) -> bool:
        return self.weekday is not None

    def applies_on(self, day: date) -> bool:
        if self.on is not None:
            return day == self.on
        if day.weekday() != self.weekday:
            return False
        if self.valid_from is not None and day < self.valid_from:
            return False
        return self.valid_to is None or day <= self.valid_to

    @property
    def span(self) -> Interval:
        return self.interval or ALL_DAY


def hours_for(day: date, hours: Iterable[OperatingHours]) -> OperatingHours | None:
    covering = [h for h in hours if h.covers(day)]
    return max(covering, key=lambda h: h.effective_from, default=None)


def scheduled_intervals(day: date, hours: Iterable[OperatingHours]) -> list[Interval]:
    """Operating hours for `day` before closures are taken out."""
    h = hours_for(day, hours)
    if h is None:
        return []
    return merge(h.intervals.get(day.weekday(), ()))


def closures_on(day: date, closures: Iterable[Closure]) -> list[Closure]:
    return [c for c in closures if c.applies_on(day)]


def open_intervals(day: date, hours: Iterable[OperatingHours], closures: Iterable[Closure]) -> list[Interval]:
    """When the location is actually open on `day`: operating hours minus closures."""
    return subtract(scheduled_intervals(day, hours), [c.span for c in closures_on(day, closures)])


def open_minutes_in_week(start: date, hours: Sequence[OperatingHours], closures: Sequence[Closure]) -> int:
    """Open minutes in the seven days from `start`, net of closures (73.5 h for the pilot)."""
    return sum(total_minutes(open_intervals(d, hours, closures)) for d in week_days(start))
