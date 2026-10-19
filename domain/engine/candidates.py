# SPDX-License-Identifier: AGPL-3.0-or-later
"""Candidate shifts (SPEC §7.2, CLAUDE.md §4): every start on the step grid and every allowed
length that fits inside one open interval (so never across a closure) and inside the person's
availability. The solver then chooses among them."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

from domain.rules import RotationMode
from domain.schedule import ScheduleContext, Shift
from domain.timeutil import Interval, intersect, subtract


@dataclass(frozen=True)
class Candidate:
    employee_id: int
    day: date
    start: int  # minutes
    end: int
    duty: bool = False  # an opener duty shift, allowed to be shorter than the minimum
    fixed: bool = False  # must stay exactly as is (locked, frozen or out of scope)
    existing_id: int | None = None

    @property
    def minutes(self) -> int:
        return self.end - self.start

    @property
    def interval(self) -> Interval:
        return Interval(self.start, self.end)

    @property
    def key(self) -> tuple[int, date, int, int]:
        return (self.employee_id, self.day, self.start, self.end)


@dataclass(frozen=True)
class Scope:
    """What a solve may change (D30, D31, D39)."""

    first_day: date | None = None
    last_day: date | None = None
    people: frozenset[int] | None = None  # None means everyone
    locked_ids: frozenset[int] = frozenset()  # published shifts, unless allowed for this run
    freeze_until: tuple[date, int] | None = None  # nothing starting before this moves or is added

    def day_in_scope(self, day: date) -> bool:
        return (self.first_day is None or day >= self.first_day) and (
            self.last_day is None or day <= self.last_day
        )

    def frozen(self, day: date, minute: int) -> bool:
        if self.freeze_until is None:
            return False
        return (day, minute) < self.freeze_until

    def shift_is_fixed(self, s: Shift) -> bool:
        return (
            (s.id is not None and s.id in self.locked_ids)
            or not self.day_in_scope(s.day)
            or (self.people is not None and s.employee_id not in self.people)
            or self.frozen(s.day, s.interval.start)
        )


@dataclass
class CandidateSet:
    candidates: list[Candidate] = field(default_factory=lambda: [])
    dropped_existing: list[Shift] = field(default_factory=lambda: [])  # existing shifts no longer allowed

    def __len__(self) -> int:
        return len(self.candidates)


def _align_up(minute: int, step: int) -> int:
    return -(-minute // step) * step


def build_candidates(
    ctx: ScheduleContext,
    scope: Scope,
    existing: Iterable[Shift] = (),
    start_step: int = 30,
    length_step: int = 30,
) -> CandidateSet:
    rules = ctx.rules
    shape = rules.shape
    out: dict[tuple[int, date, int, int], Candidate] = {}
    result = CandidateSet()

    def add(c: Candidate) -> None:
        prev = out.get(c.key)
        if prev is None or (c.fixed and not prev.fixed) or (c.existing_id and not prev.existing_id):
            out[c.key] = Candidate(
                c.employee_id,
                c.day,
                c.start,
                c.end,
                duty=c.duty or (prev.duty if prev else False),
                fixed=c.fixed or (prev.fixed if prev else False),
                existing_id=c.existing_id or (prev.existing_id if prev else None),
            )

    # Existing assigned shifts: fixed ones stay as they are; free ones are offered again
    # (warm start) if they still fit the person's availability and the location's hours.
    for s in existing:
        if s.employee_id is None:
            continue
        cand = Candidate(s.employee_id, s.day, s.interval.start, s.interval.end, existing_id=s.id)
        if scope.shift_is_fixed(s):
            add(Candidate(cand.employee_id, cand.day, cand.start, cand.end, fixed=True, existing_id=s.id))
            continue
        person = ctx.people.get(s.employee_id)
        open_ivs = ctx.open_on(s.day)
        fits_hours = any(iv.contains(s.interval) for iv in open_ivs)
        if person is None or not person.auto_schedulable or not fits_hours:
            result.dropped_existing.append(s)
            continue
        if (
            person.availability.is_known
            and person.availability.conflicting_blocks(s.day.weekday(), s.interval)
        ) or intersect([s.interval], person.off_on(s.day) + _busy_elsewhere(ctx, person.id, s.day)):
            result.dropped_existing.append(s)
            continue
        add(cand)

    for person in ctx.people.values():
        if not person.auto_schedulable:
            continue  # non_regular and inactive are never auto-scheduled (scenario 7)
        openers = [o for o in rules.openers if o.employee_id == person.id]
        for day in ctx.period.days:
            if not scope.day_in_scope(day):
                continue
            week = ctx.period.week_of(day)
            if rules.rotation.mode == RotationMode.STRICT and not rules.rotation.on_week(person.id, week):
                continue
            open_ivs = ctx.open_on(day)
            wd = day.weekday()
            if person.availability.is_known:
                avail = person.availability.available_within(wd, open_ivs)
            else:
                avail = open_ivs  # not_entered: allowed but heavily penalised (SPEC §5.1)
            avail = subtract(avail, person.off_on(day))  # approved time off is never scheduled
            avail = subtract(avail, _busy_elsewhere(ctx, person.id, day))  # nor time on another schedule
            for iv in avail:
                for start in range(_align_up(iv.start, start_step), iv.end, start_step):
                    if scope.frozen(day, start):
                        continue
                    for length in range(shape.min_shift, shape.max_shift + 1, length_step):
                        if start + length > iv.end:
                            break
                        add(Candidate(person.id, day, start, start + length))
            # Opener duty candidates: start at opening time, short on the listed weekdays.
            for o in openers:
                if wd not in o.weekdays or not open_ivs:
                    continue
                opening = open_ivs[0].start
                lengths = [o.short_minutes] if wd in o.short_weekdays else []
                lengths += list(range(shape.min_shift, shape.max_shift + 1, length_step))
                for length in lengths:
                    shift_iv = Interval(opening, min(opening + length, 1440))
                    if scope.frozen(day, opening) or not any(a.contains(shift_iv) for a in avail):
                        continue
                    add(Candidate(person.id, day, shift_iv.start, shift_iv.end, duty=True))
    result.candidates = sorted(out.values(), key=lambda c: (c.day, c.employee_id, c.start, c.end))
    return result


def _busy_elsewhere(ctx: ScheduleContext, employee_id: int, day: date) -> list[Interval]:
    """Time taken by shifts on other schedules, widened by the minimum gap between shifts (D95)."""
    gap = ctx.rules.shape.min_gap
    return [
        Interval(max(0, s.interval.start - gap), s.interval.end + gap)
        for s in ctx.elsewhere_on(employee_id, day)
    ]
