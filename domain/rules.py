# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scheduling rules (SPEC §6), all configuration. Times are minutes; hours are converted
at the edges so nothing here rounds."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
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
        return f"A {self.tag} is never the only person on duty"


@dataclass(frozen=True)
class ShapeException:
    """A per-person exception to the shift shape, for example the opener's 1.5-hour shift on
    days with a 9am class (SPEC §6.1)."""

    employee_id: int
    weekdays: frozenset[int]
    min_shift: int


@dataclass(frozen=True)
class Rules:
    shape: ShapeRules = field(default_factory=ShapeRules)
    coverage: CoverageRules = field(default_factory=CoverageRules)
    caps: Sequence[HourCap] = ()
    tag_rules: Sequence[TagRule] = ()
    shape_exceptions: Sequence[ShapeException] = ()

    def min_shift_for(self, employee_id: int | None, day: date) -> int:
        for e in self.shape_exceptions:
            if e.employee_id == employee_id and day.weekday() in e.weekdays:
                return min(e.min_shift, self.shape.min_shift)
        return self.shape.min_shift
