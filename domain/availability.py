# SPDX-License-Identifier: AGPL-3.0-or-later
"""Weekly availability (SPEC §5).

Time not covered by an `unavailable` block counts as available, but only once the
person has entered their availability: `not_entered` is never the same as "fully
available" (SPEC §5.1, CLAUDE.md §7). Entered or imported times are kept exactly;
for scheduling they round toward unavailability, so an 8:50 end blocks until 9:00
(SPEC §3.3).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

from domain.calendar import WEEKDAY_SHORT, Closure, OperatingHours, open_intervals, week_days
from domain.timeutil import (
    SLOT_MINUTES,
    Interval,
    intersect,
    merge,
    slot_ceil,
    slot_floor,
    subtract,
    total_minutes,
)


class AvailabilityStatus(StrEnum):
    ENTERED = "entered"
    NOT_ENTERED = "not_entered"
    NEEDS_REVIEW = "needs_review"


class BlockKind(StrEnum):
    UNAVAILABLE = "unavailable"
    PREFERRED = "preferred"


@dataclass(frozen=True, order=True)
class AvailabilityBlock:
    weekday: int
    kind: BlockKind
    interval: Interval


@dataclass(frozen=True)
class WeeklyAvailability:
    status: AvailabilityStatus
    blocks: Sequence[AvailabilityBlock] = field(default_factory=tuple[AvailabilityBlock, ...])

    @property
    def is_known(self) -> bool:
        """Whether there's anything to go on. `needs_review` is known but unconfirmed."""
        return self.status != AvailabilityStatus.NOT_ENTERED

    def exact(self, weekday: int, kind: BlockKind) -> list[Interval]:
        """Blocks of one kind on one weekday, as entered (for display and validation)."""
        return merge(b.interval for b in self.blocks if b.weekday == weekday and b.kind == kind)

    def unavailable_for_scheduling(self, weekday: int) -> list[Interval]:
        """Unavailable time widened to whole slots: starts round down, ends round up."""
        return merge(
            Interval(slot_floor(iv.start) * SLOT_MINUTES, slot_ceil(iv.end) * SLOT_MINUTES)
            for iv in self.exact(weekday, BlockKind.UNAVAILABLE)
        )

    def preferred_for_scheduling(self, weekday: int) -> list[Interval]:
        """Preferred time narrowed to whole slots and never overlapping unavailability."""
        narrowed: list[Interval] = []
        for iv in self.exact(weekday, BlockKind.PREFERRED):
            start, end = slot_ceil(iv.start) * SLOT_MINUTES, slot_floor(iv.end) * SLOT_MINUTES
            if start < end:
                narrowed.append(Interval(start, end))
        return subtract(narrowed, self.unavailable_for_scheduling(weekday))

    def available_within(self, weekday: int, open_ivs: Iterable[Interval]) -> list[Interval]:
        """Scheduling-time availability inside the given open intervals. Empty when not entered."""
        if not self.is_known:
            return []
        return subtract(open_ivs, self.unavailable_for_scheduling(weekday))


def normalise_blocks(blocks: Iterable[AvailabilityBlock]) -> list[AvailabilityBlock]:
    """Merge overlapping blocks of the same kind and day, and drop preferred time that is
    also marked unavailable (unavailable wins)."""
    out: list[AvailabilityBlock] = []
    blocks = list(blocks)
    for wd in range(7):
        un = merge(b.interval for b in blocks if b.weekday == wd and b.kind == BlockKind.UNAVAILABLE)
        pref = subtract((b.interval for b in blocks if b.weekday == wd and b.kind == BlockKind.PREFERRED), un)
        out += [AvailabilityBlock(wd, BlockKind.UNAVAILABLE, iv) for iv in un]
        out += [AvailabilityBlock(wd, BlockKind.PREFERRED, iv) for iv in pref]
    return out


@dataclass(frozen=True)
class DaySummary:
    day: date
    open: list[Interval]
    available: list[Interval]
    unavailable: list[Interval]  # exact times inside opening hours, for display
    preferred: list[Interval]

    @property
    def open_minutes(self) -> int:
        return total_minutes(self.open)

    @property
    def available_minutes(self) -> int:
        return total_minutes(self.available)

    @property
    def fully_out(self) -> bool:
        return self.open_minutes > 0 and self.available_minutes == 0

    @property
    def fully_free(self) -> bool:
        return self.open_minutes > 0 and self.available_minutes == self.open_minutes


@dataclass(frozen=True)
class WeekSummary:
    """Per-employee row of the availability grid (SPEC §5.3)."""

    status: AvailabilityStatus
    days: list[DaySummary]

    @property
    def open_minutes(self) -> int:
        return sum(d.open_minutes for d in self.days)

    @property
    def available_minutes(self) -> int:
        return sum(d.available_minutes for d in self.days)

    @property
    def days_fully_out(self) -> list[str]:
        return [WEEKDAY_SHORT[d.day.weekday()] for d in self.days if d.fully_out]


def summarise_week(
    availability: WeeklyAvailability,
    week_start: date,
    hours: Sequence[OperatingHours],
    closures: Sequence[Closure],
) -> WeekSummary:
    days: list[DaySummary] = []
    for d in week_days(week_start):
        open_ivs = open_intervals(d, hours, closures)
        wd = d.weekday()
        days.append(
            DaySummary(
                day=d,
                open=open_ivs,
                available=availability.available_within(wd, open_ivs),
                unavailable=intersect(availability.exact(wd, BlockKind.UNAVAILABLE), open_ivs),
                preferred=intersect(availability.preferred_for_scheduling(wd), open_ivs),
            )
        )
    return WeekSummary(availability.status, days)


@dataclass(frozen=True)
class DayCoverage:
    day: date
    free_all_open_hours: int
    free_part: int
    not_entered: int


def coverage_summary(summaries: Sequence[WeekSummary]) -> list[DayCoverage]:
    """How many people are free for all of each day's open hours, and how many for part
    (SPEC §5.3). People without entered availability are counted separately, never as free."""
    if not summaries:
        return []
    out: list[DayCoverage] = []
    for i, d in enumerate(summaries[0].days):
        rows = [s.days[i] for s in summaries]
        known = [
            (s, r) for s, r in zip(summaries, rows, strict=True) if s.status != AvailabilityStatus.NOT_ENTERED
        ]
        out.append(
            DayCoverage(
                day=d.day,
                free_all_open_hours=sum(1 for _, r in known if r.fully_free),
                free_part=sum(1 for _, r in known if r.available_minutes and not r.fully_free),
                not_entered=len(summaries) - len(known),
            )
        )
    return out
