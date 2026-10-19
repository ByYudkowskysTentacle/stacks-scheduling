# SPDX-License-Identifier: AGPL-3.0-or-later
"""Diffs between a starting schedule and a solver result, with shift IDs kept where a shift
was retimed or reassigned rather than replaced (CLAUDE.md §2.3, D38)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from domain.schedule import Period, Shift
from domain.timeutil import Interval


class ChangeKind(StrEnum):
    KEPT = "kept"
    RETIMED = "retimed"
    REASSIGNED = "reassigned"
    REMOVED = "removed"
    ADDED = "added"


@dataclass(frozen=True)
class Change:
    kind: ChangeKind
    old: Shift | None
    new: Shift | None


def _overlap(a: Interval, b: Interval) -> int:
    return max(0, min(a.end, b.end) - max(a.start, b.start))


def match(old: Sequence[Shift], new: Sequence[Shift]) -> tuple[list[Shift], list[Change]]:
    """Give result shifts the IDs of the shifts they continue. Order of preference: the same
    shift unchanged, then the same person's overlapping shift that day (a retime), then the
    same day and time for someone else (a reassignment)."""
    old_left = [s for s in old if s.employee_id is not None]
    new_left = list(new)
    out: list[Shift] = []
    changes: list[Change] = []

    def take(o: Shift, n: Shift, kind: ChangeKind) -> None:
        old_left.remove(o)
        new_left.remove(n)
        # A reassigned shift takes the new person's default pay role, set by the caller (scenario 15).
        pay_role = o.pay_role_id if n.employee_id == o.employee_id else None
        kept = Shift(o.id, n.employee_id, n.day, n.interval, pay_role, o.location_id, n.locked)
        out.append(kept)
        changes.append(Change(kind, o, kept))

    for o in list(old_left):
        n = next(
            (
                n
                for n in new_left
                if n.employee_id == o.employee_id and n.day == o.day and n.interval == o.interval
            ),
            None,
        )
        if n is not None:
            take(o, n, ChangeKind.KEPT)
    pairs = sorted(
        (
            (_overlap(o.interval, n.interval), o, n)
            for o in old_left
            for n in new_left
            if o.employee_id == n.employee_id and o.day == n.day and o.interval.overlaps(n.interval)
        ),
        key=lambda t: -t[0],
    )
    for _, o, n in pairs:
        if o in old_left and n in new_left:
            take(o, n, ChangeKind.RETIMED)
    for o in list(old_left):
        n = next((n for n in new_left if n.day == o.day and n.interval == o.interval), None)
        if n is not None:
            take(o, n, ChangeKind.REASSIGNED)
    for o in old_left:
        changes.append(Change(ChangeKind.REMOVED, o, None))
    for n in new_left:
        out.append(n)
        changes.append(Change(ChangeKind.ADDED, None, n))
    return out, changes


def moved_minutes(old: Iterable[Shift], new: Iterable[Shift]) -> int:
    """Hours moved (D33), in minutes: a removed or reassigned shift counts in full; a retimed
    one only the part that no longer overlaps its old time; added shifts don't count."""
    by_person_day: dict[tuple[int | None, object], list[Interval]] = defaultdict(list)
    for n in new:
        by_person_day[(n.employee_id, n.day)].append(n.interval)
    total = 0
    for o in old:
        if o.employee_id is None:
            continue
        still = sum(_overlap(o.interval, iv) for iv in by_person_day.get((o.employee_id, o.day), []))
        total += o.minutes - min(still, o.minutes)
    return total


@dataclass(frozen=True)
class HoursChange:
    employee_id: int
    before: tuple[int, ...]  # minutes per week
    after: tuple[int, ...]

    @property
    def delta(self) -> int:
        return sum(self.after) - sum(self.before)


def hours_changes(old: Iterable[Shift], new: Iterable[Shift], period: Period) -> list[HoursChange]:
    def per_week(shifts: Iterable[Shift]) -> dict[int, list[int]]:
        out: dict[int, list[int]] = defaultdict(lambda: [0] * period.weeks)
        for s in shifts:
            if s.employee_id is not None and period.start <= s.day <= period.end:
                out[s.employee_id][period.week_of(s.day)] += s.minutes
        return out

    before, after = per_week(old), per_week(new)
    rows: list[HoursChange] = []
    for emp in sorted(set(before) | set(after)):
        b = tuple(before.get(emp, [0] * period.weeks))
        a = tuple(after.get(emp, [0] * period.weeks))
        if a != b:
            rows.append(HoursChange(emp, b, a))
    return sorted(rows, key=lambda r: r.delta)
