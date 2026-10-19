# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scheduling rules (SPEC §6), all configuration. Times are minutes; hours are converted
at the edges so nothing here rounds."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from enum import StrEnum

from domain.timeutil import Interval


@dataclass(frozen=True)
class ShapeRules:
    """Shift shape (SPEC §6.1), with the pilot's defaults."""

    min_shift: int = 120
    max_shift: int = 240
    max_daily: int = 360
    max_shifts_per_day: int = 2
    min_gap: int = 30


class CoverageMode(StrEnum):
    EXACT = "exact"  # under and over both count
    MINIMUM = "minimum"  # only under counts


@dataclass(frozen=True)
class CoverageOverride:
    weekdays: frozenset[int]
    interval: Interval
    count: int


@dataclass(frozen=True)
class CoverageRules:
    """People on duty per slot (SPEC §6.2). The last matching override wins."""

    default: int = 2
    mode: CoverageMode = CoverageMode.EXACT
    overrides: Sequence[CoverageOverride] = ()

    def target(self, weekday: int, minute: int) -> int:
        count = self.default
        for o in self.overrides:
            if weekday in o.weekdays and o.interval.start <= minute < o.interval.end:
                count = o.count
        return count


class CapScope(StrEnum):
    WEEK = "week"
    PERIOD = "period"
    TERM = "term"


@dataclass(frozen=True)
class HourCap:
    """A maximum on scheduled minutes (SPEC §6.4). Applies to one employee, or to everyone
    holding a tag during the span, or (neither set) to everyone."""

    scope: CapScope
    minutes: int
    employee_id: int | None = None
    tag: str | None = None
    label: str = ""


class TagRuleKind(StrEnum):
    MIN_ON_DUTY = "min_on_duty"  # at least `count` people with the tag on duty in the window
    MIN_WEEKLY_HOURS = "min_weekly_hours"  # tagged people's scheduled minutes per week
    NOT_ALONE = "not_alone"  # a tagged person always has someone without the tag on duty too
    NOT_SOLO = "not_solo"  # a tagged person is never the only one on duty; anyone else counts (D96)


@dataclass(frozen=True)
class TagRule:
    """Skill coverage rules (SPEC §6.3). Hard rules are constraints for the generator; the
    validation suite reports both kinds."""

    tag: str
    kind: TagRuleKind
    hard: bool = False
    count: int = 1
    minutes: int = 0
    weekdays: frozenset[int] = frozenset(range(7))
    interval: Interval | None = None  # None means all open hours
    label: str = ""

    def describe(self) -> str:
        if self.label:
            return self.label
        if self.kind == TagRuleKind.MIN_ON_DUTY:
            when = f" {self.interval.label()}" if self.interval else " during open hours"
            return f"At least {self.count} {self.tag} on duty{when}"
        if self.kind == TagRuleKind.MIN_WEEKLY_HOURS:
            return f"{self.tag} hours total at least {self.minutes / 60:g} per week"
        if self.kind == TagRuleKind.NOT_SOLO:
            return f"A {self.tag} never works alone"
        return f"A {self.tag} always has someone without the tag on duty"


@dataclass(frozen=True)
class ShapeException:
    """A per-person exception to the shift shape, for example the opener's 1.5-hour shift on
    days with a 9am class (SPEC §6.1)."""

    employee_id: int
    weekdays: frozenset[int]
    min_shift: int


class RotationMode(StrEnum):
    NONE = "none"
    STRICT = "strict"  # members work only in their group's weeks
    PREFERRED = "preferred"  # off-week shifts allowed but penalised


@dataclass(frozen=True)
class Rotation:
    """Week-on, week-off groups (SPEC §6.5). Group `groups[i]` works the weeks where
    `week % len(groups) == i`; people not in a group work any week."""

    mode: RotationMode = RotationMode.NONE
    members: Mapping[int, str] = field(default_factory=dict[int, str])
    groups: tuple[str, ...] = ("A", "B")

    def on_week(self, employee_id: int, week: int) -> bool:
        group = self.members.get(employee_id)
        if self.mode == RotationMode.NONE or group not in self.groups:
            return True
        return week % len(self.groups) == self.groups.index(group)


@dataclass(frozen=True)
class OpenerDuty:
    """ "Student L opens every weekday" (scenario 3): a shift starting at opening time on each
    listed weekday. On `short_weekdays` the shift may be as short as `short_minutes`, a duty
    exception to the minimum shift length (SPEC §6.1)."""

    employee_id: int
    weekdays: frozenset[int]
    short_weekdays: frozenset[int] = frozenset()
    short_minutes: int = 90


@dataclass(frozen=True)
class WeekHoursDuty:
    """ "Lead works 20 hours in their lead week and no more than 10 in the other" (scenario 4).
    `weeks` are period week indexes (0-based)."""

    employee_id: int
    weeks: frozenset[int]
    minutes: int
    off_week_max: int
    label: str = "lead week"


@dataclass(frozen=True)
class HourTarget:
    """A supervisor's soft goal, for example "as close to 20 as possible" (SPEC §6.4).
    Priority multiplies its weight. `weeks` None means every week of the period."""

    employee_id: int
    minutes: int
    priority: int = 1
    weeks: frozenset[int] | None = None


@dataclass(frozen=True)
class DesiredHours:
    """Approved weekly hours an employee asked for (D32). Only approved values are passed in.
    `work_study` marks a student with award remaining, who comes first (D34)."""

    employee_id: int
    minutes: int
    work_study: bool = False


@dataclass(frozen=True)
class Rules:
    shape: ShapeRules = field(default_factory=ShapeRules)
    coverage: CoverageRules = field(default_factory=CoverageRules)
    caps: Sequence[HourCap] = ()
    tag_rules: Sequence[TagRule] = ()
    shape_exceptions: Sequence[ShapeException] = ()
    rotation: Rotation = field(default_factory=Rotation)
    openers: Sequence[OpenerDuty] = ()
    week_duties: Sequence[WeekHoursDuty] = ()
    targets: Sequence[HourTarget] = ()
    desired: Sequence[DesiredHours] = ()

    def min_shift_for(self, employee_id: int | None, day: date) -> int:
        lowest = self.shape.min_shift
        for e in self.shape_exceptions:
            if e.employee_id == employee_id and day.weekday() in e.weekdays:
                lowest = min(lowest, e.min_shift)
        for o in self.openers:
            if o.employee_id == employee_id and day.weekday() in o.short_weekdays:
                lowest = min(lowest, o.short_minutes)
        return lowest

    def duty_employee_ids(self) -> set[int]:
        return {o.employee_id for o in self.openers} | {d.employee_id for d in self.week_duties}

    def softened(self, description: str) -> Rules:
        """These rules with the hard tag rule of that description treated as a preference, below
        coverage: the "relax this rule, keeping coverage" what-if (D97)."""
        return replace(
            self,
            tag_rules=[
                replace(r, hard=False) if r.hard and r.describe() == description else r
                for r in self.tag_rules
            ],
        )
