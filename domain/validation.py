# SPDX-License-Identifier: AGPL-3.0-or-later
"""The validation suite (SPEC §8). Run after every schedule mutation; a clean run still
reports "0 issues" (CLAUDE.md §2.4).

Errors break a hard rule, warnings break a soft rule or need a supervisor's eye, and
info items (drift, open shifts) are reported but not counted as issues.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from itertools import pairwise

from domain.availability import AvailabilityStatus
from domain.calendar import WEEKDAY_SHORT
from domain.rules import CapScope, CoverageMode, HourCap, TagRule, TagRuleKind
from domain.schedule import (
    Person,
    ScheduleContext,
    Shift,
    minutes_by_person_week,
    on_duty_by_slot,
    runs,
    slots_of,
)
from domain.timeutil import SLOT_MINUTES, Interval, intersect, is_on_slot, subtract, total_minutes


class Category(StrEnum):
    COVERAGE_GAP = "coverage_gap"
    OVER_COVERAGE = "over_coverage"
    CLOSED = "closed"
    AVAILABILITY = "availability"
    SHAPE = "shape"
    CAP = "cap"
    TAG_RULE = "tag_rule"
    DUTY = "duty"
    DOUBLE_BOOKING = "double_booking"
    STATUS = "status"
    DRIFT = "drift"
    OPEN_SHIFT = "open_shift"


CATEGORY_LABELS = {
    Category.COVERAGE_GAP: ("coverage gap", "coverage gaps"),
    Category.OVER_COVERAGE: ("over-coverage", "over-coverage"),
    Category.CLOSED: ("shift outside open hours", "shifts outside open hours"),
    Category.AVAILABILITY: ("availability conflict", "availability conflicts"),
    Category.SHAPE: ("shift shape issue", "shift shape issues"),
    Category.CAP: ("cap breach", "cap breaches"),
    Category.TAG_RULE: ("tag rule unmet", "tag rules unmet"),
    Category.DUTY: ("duty unmet", "duties unmet"),
    Category.DOUBLE_BOOKING: ("double booking", "double bookings"),
    Category.STATUS: ("availability status warning", "availability status warnings"),
    Category.DRIFT: ("change since publishing", "changes since publishing"),
    Category.OPEN_SHIFT: ("open shift", "open shifts"),
}


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True)
class Issue:
    category: Category
    severity: Severity
    message: str
    day: date | None = None
    interval: Interval | None = None
    employee_id: int | None = None
    shift_id: int | None = None
    minutes: int = 0  # size of the problem in minutes, where that makes sense


@dataclass
class Report:
    issues: list[Issue] = field(default_factory=lambda: [])

    @property
    def counted(self) -> list[Issue]:
        return [i for i in self.issues if i.severity != Severity.INFO]

    @property
    def info(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == Severity.INFO]

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == Severity.ERROR]

    def by_category(self) -> dict[Category, list[Issue]]:
        out: dict[Category, list[Issue]] = defaultdict(list)
        for i in self.issues:
            out[i.category].append(i)
        return dict(out)

    def summary(self) -> str:
        """One line for flash messages: "0 issues" or "3 issues: 2 coverage gaps, 1 cap breach"."""
        n = len(self.counted)
        if n == 0:
            return "0 issues"
        parts: list[str] = []
        for category, count in Counter(i.category for i in self.counted).most_common():
            singular, plural = CATEGORY_LABELS[category]
            parts.append(f"{count} {singular if count == 1 else plural}")
        return f"{n} issue{'s' if n != 1 else ''}: {', '.join(parts)}"


def hours(minutes: int) -> str:
    return f"{minutes / 60:g}h"


def day_label(day: date) -> str:
    return f"{WEEKDAY_SHORT[day.weekday()]} {day:%-d %b}"


def name(ctx: ScheduleContext, employee_id: int | None) -> str:
    if employee_id is None:
        return "Open shift"
    person = ctx.people.get(employee_id)
    return person.name if person else f"Employee #{employee_id}"


# --- 1. Coverage ----------------------------------------------------------------------------


def check_coverage(ctx: ScheduleContext) -> list[Issue]:
    issues: list[Issue] = []
    duty = on_duty_by_slot(ctx.shifts)
    cov = ctx.rules.coverage
    for day in ctx.period.days:
        open_ivs = ctx.open_on(day)
        under: dict[int, list[int]] = defaultdict(list)
        over: dict[int, list[int]] = defaultdict(list)
        for iv in open_ivs:
            for slot in slots_of(iv):
                target = cov.target(day.weekday(), slot * SLOT_MINUTES)
                have = len(duty[day][slot])
                if have < target:
                    under[target - have].append(slot)
                elif have > target and cov.mode == CoverageMode.EXACT:
                    over[have - target].append(slot)
        for short, slots in sorted(under.items()):
            for iv in runs(slots):
                issues.append(
                    Issue(
                        Category.COVERAGE_GAP,
                        Severity.ERROR,
                        f"{day_label(day)} {iv.label()}: {short} {'person' if short == 1 else 'people'} "
                        "short",
                        day=day,
                        interval=iv,
                        minutes=iv.minutes * short,
                    )
                )
        for extra, slots in sorted(over.items()):
            for iv in runs(slots):
                issues.append(
                    Issue(
                        Category.OVER_COVERAGE,
                        Severity.WARNING,
                        f"{day_label(day)} {iv.label()}: {extra} more than needed",
                        day=day,
                        interval=iv,
                        minutes=iv.minutes * extra,
                    )
                )
        # Closures and closed hours must have nobody on duty.
        for s in ctx.shifts:
            if s.day != day:
                continue
            outside = subtract([s.interval], open_ivs)
            if outside:
                spans = ", ".join(iv.label() for iv in outside)
                issues.append(
                    Issue(
                        Category.CLOSED,
                        Severity.ERROR,
                        f"{name(ctx, s.employee_id)}'s shift {day_label(day)} {s.interval.label()} "
                        f"runs while closed ({spans})",
                        day=day,
                        interval=s.interval,
                        employee_id=s.employee_id,
                        shift_id=s.id,
                        minutes=total_minutes(outside),
                    )
                )
    return issues


# --- 2. Availability and 7. status ----------------------------------------------------------


def check_availability(ctx: ScheduleContext) -> list[Issue]:
    issues: list[Issue] = []
    flagged_status: set[int] = set()
    for s in ctx.assigned():
        person = ctx.people.get(s.employee_id)  # type: ignore[arg-type]
        if person is None:
            continue
        off = intersect([s.interval], person.off_on(s.day))
        if off:
            issues.append(
                Issue(
                    Category.AVAILABILITY,
                    Severity.ERROR,
                    f"{person.name} has approved time off {day_label(s.day)} "
                    f"{', '.join(iv.label() for iv in off)} (shift {s.interval.label()})",
                    day=s.day,
                    interval=s.interval,
                    employee_id=person.id,
                    shift_id=s.id,
                    minutes=total_minutes(off),
                )
            )
        a = person.availability
        if a.status == AvailabilityStatus.NOT_ENTERED:
            if person.id not in flagged_status:
                flagged_status.add(person.id)
                issues.append(
                    Issue(
                        Category.STATUS,
                        Severity.WARNING,
                        f"{person.name} is scheduled but hasn't entered availability",
                        employee_id=person.id,
                    )
                )
            continue
        clash = intersect([s.interval], a.unavailable_for_scheduling(s.day.weekday()))
        if clash:
            spans = ", ".join(iv.label() for iv in a.conflicting_blocks(s.day.weekday(), s.interval))
            issues.append(
                Issue(
                    Category.AVAILABILITY,
                    Severity.ERROR,
                    f"{person.name} is unavailable {day_label(s.day)} {spans} (shift {s.interval.label()})",
                    day=s.day,
                    interval=s.interval,
                    employee_id=person.id,
                    shift_id=s.id,
                    minutes=total_minutes(clash),
                )
            )
        if a.status == AvailabilityStatus.NEEDS_REVIEW and person.id not in flagged_status:
            flagged_status.add(person.id)
            issues.append(
                Issue(
                    Category.STATUS,
                    Severity.INFO,
                    f"{person.name}'s availability was imported and hasn't been confirmed",
                    employee_id=person.id,
                )
            )
    return issues


# --- 3. Shape and 6. double booking ---------------------------------------------------------


def check_shape(ctx: ScheduleContext) -> list[Issue]:
    issues: list[Issue] = []
    shape = ctx.rules.shape
    by_person_day: dict[tuple[int, date], list[Shift]] = defaultdict(list)
    for s in ctx.shifts:
        who = name(ctx, s.employee_id)
        where = f"{who} {day_label(s.day)} {s.interval.label()}"
        if not (is_on_slot(s.interval.start) and is_on_slot(s.interval.end)):
            issues.append(
                Issue(
                    Category.SHAPE,
                    Severity.ERROR,
                    f"{where}: not on 15-minute boundaries",
                    s.day,
                    s.interval,
                    s.employee_id,
                    s.id,
                )
            )
        min_len = ctx.rules.min_shift_for(s.employee_id, s.day)
        if s.minutes < min_len:
            issues.append(
                Issue(
                    Category.SHAPE,
                    Severity.ERROR,
                    f"{where}: shorter than {hours(min_len)}",
                    s.day,
                    s.interval,
                    s.employee_id,
                    s.id,
                    min_len - s.minutes,
                )
            )
        if s.minutes > shape.max_shift:
            issues.append(
                Issue(
                    Category.SHAPE,
                    Severity.ERROR,
                    f"{where}: longer than {hours(shape.max_shift)}",
                    s.day,
                    s.interval,
                    s.employee_id,
                    s.id,
                    s.minutes - shape.max_shift,
                )
            )
        if s.employee_id is not None:
            by_person_day[(s.employee_id, s.day)].append(s)
    other = {id(s) for s in ctx.elsewhere}
    for (emp, day), own in list(by_person_day.items()):
        own.extend(ctx.elsewhere_on(emp, day))

    def label(s: Shift) -> str:
        return s.interval.label() + (" on another schedule" if id(s) in other else "")

    for (emp, day), shifts in sorted(by_person_day.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        who = f"{name(ctx, emp)} {day_label(day)}"
        shifts.sort(key=lambda s: s.interval)
        total = sum(s.minutes for s in shifts)
        if total > shape.max_daily:
            issues.append(
                Issue(
                    Category.SHAPE,
                    Severity.ERROR,
                    f"{who}: {hours(total)} in a day, more than {hours(shape.max_daily)}",
                    day,
                    employee_id=emp,
                    minutes=total - shape.max_daily,
                )
            )
        if len(shifts) > shape.max_shifts_per_day:
            issues.append(
                Issue(
                    Category.SHAPE,
                    Severity.ERROR,
                    f"{who}: {len(shifts)} shifts, more than {shape.max_shifts_per_day}",
                    day,
                    employee_id=emp,
                )
            )
        for a, b in pairwise(shifts):
            if id(a) in other and id(b) in other:
                continue  # both on another schedule: that schedule reports it
            mine = b if id(b) not in other else a  # point at the shift on this schedule
            if a.interval.overlaps(b.interval):
                issues.append(
                    Issue(
                        Category.DOUBLE_BOOKING,
                        Severity.ERROR,
                        f"{who}: {label(a)} overlaps {label(b)}",
                        day,
                        mine.interval,
                        emp,
                        mine.id,
                    )
                )
            elif b.interval.start - a.interval.end < shape.min_gap:
                issues.append(
                    Issue(
                        Category.SHAPE,
                        Severity.ERROR,
                        f"{who}: only {b.interval.start - a.interval.end} minutes between shifts "
                        f"(at least {shape.min_gap} needed)",
                        day,
                        mine.interval,
                        emp,
                        mine.id,
                    )
                )
    return issues


# --- 4. Caps --------------------------------------------------------------------------------


def _cap_applies(cap: HourCap, person: Person, days: list[date]) -> bool:
    if cap.employee_id is not None:
        return cap.employee_id == person.id
    if cap.tag is not None:
        return person.tagged_in(cap.tag, days)
    return True


def _cap_label(cap: HourCap) -> str:
    if cap.label:
        return cap.label
    who = "personal" if cap.employee_id else (f"{cap.tag}" if cap.tag else "default")
    return f"{who} {cap.scope.value} cap of {hours(cap.minutes)}"


def check_caps(ctx: ScheduleContext) -> list[Issue]:
    issues: list[Issue] = []
    own_weeks = minutes_by_person_week(ctx.shifts, ctx.period)
    # Weekly and period caps count hours on other schedules too (D95); term caps already do,
    # through term_minutes_outside_period.
    per_week = minutes_by_person_week([*ctx.shifts, *ctx.elsewhere], ctx.period)
    for emp, own in sorted(own_weeks.items()):
        person = ctx.people.get(emp)
        if person is None:
            continue
        weeks = {w: m for w, m in per_week[emp].items() if w in own}
        for cap in ctx.rules.caps:
            if cap.scope == CapScope.WEEK:
                for week, used in sorted(weeks.items()):
                    days = ctx.period.week_days(week)
                    if _cap_applies(cap, person, days) and used > cap.minutes:
                        issues.append(
                            Issue(
                                Category.CAP,
                                Severity.ERROR,
                                f"{person.name}: {hours(used)} in {ctx.period.week_label(week)}, over "
                                f"the {_cap_label(cap)}",
                                employee_id=emp,
                                minutes=used - cap.minutes,
                            )
                        )
            elif cap.scope == CapScope.PERIOD:
                used = sum(per_week[emp].values())
                if _cap_applies(cap, person, ctx.period.days) and used > cap.minutes:
                    issues.append(
                        Issue(
                            Category.CAP,
                            Severity.ERROR,
                            f"{person.name}: {hours(used)} this period, over the {_cap_label(cap)}",
                            employee_id=emp,
                            minutes=used - cap.minutes,
                        )
                    )
            elif cap.scope == CapScope.TERM:
                used = sum(own.values()) + ctx.term_minutes_outside_period.get(emp, 0)
                if _cap_applies(cap, person, ctx.period.days) and used > cap.minutes:
                    issues.append(
                        Issue(
                            Category.CAP,
                            Severity.ERROR,
                            f"{person.name}: {hours(used)} this term, over the {_cap_label(cap)}",
                            employee_id=emp,
                            minutes=used - cap.minutes,
                        )
                    )
    return issues


def effective_weekly_cap(ctx: ScheduleContext, person: Person, week: int) -> int | None:
    """The tightest weekly cap for a person in a week, for hours reports."""
    days = ctx.period.week_days(week)
    caps = [c.minutes for c in ctx.rules.caps if c.scope == CapScope.WEEK and _cap_applies(c, person, days)]
    return min(caps) if caps else None


def effective_period_cap(ctx: ScheduleContext, person: Person) -> int | None:
    caps = [
        c.minutes
        for c in ctx.rules.caps
        if c.scope == CapScope.PERIOD and _cap_applies(c, person, ctx.period.days)
    ]
    return min(caps) if caps else None


# --- 5. Tag rules ---------------------------------------------------------------------------


def _rule_window(ctx: ScheduleContext, rule: TagRule, day: date) -> list[Interval]:
    if day.weekday() not in rule.weekdays:
        return []
    open_ivs = ctx.open_on(day)
    return intersect(open_ivs, [rule.interval]) if rule.interval else open_ivs


def check_tag_rules(ctx: ScheduleContext) -> list[Issue]:
    issues: list[Issue] = []
    duty = on_duty_by_slot(ctx.shifts)
    for rule in ctx.rules.tag_rules:
        severity = Severity.ERROR if rule.hard else Severity.WARNING
        if rule.kind == TagRuleKind.MIN_ON_DUTY:
            for day in ctx.period.days:
                short: list[int] = []
                for iv in _rule_window(ctx, rule, day):
                    for slot in slots_of(iv):
                        tagged = sum(
                            1
                            for e in duty[day][slot]
                            if (p := ctx.people.get(e)) and p.has_tag(rule.tag, day)
                        )
                        if tagged < rule.count:
                            short.append(slot)
                for iv in runs(short):
                    issues.append(
                        Issue(
                            Category.TAG_RULE,
                            severity,
                            f"{rule.describe()}: none on duty {day_label(day)} {iv.label()}"
                            if rule.count == 1
                            else f"{rule.describe()}: fewer than {rule.count} on duty {day_label(day)} "
                            f"{iv.label()}",
                            day,
                            iv,
                            minutes=iv.minutes,
                        )
                    )
        elif rule.kind == TagRuleKind.MIN_WEEKLY_HOURS:
            for week in range(ctx.period.weeks):
                days = set(ctx.period.week_days(week))
                total = sum(
                    s.minutes
                    for s in ctx.assigned()
                    if s.day in days and (p := ctx.people.get(s.employee_id)) and p.has_tag(rule.tag, s.day)  # type: ignore[arg-type]
                )
                if total < rule.minutes:
                    issues.append(
                        Issue(
                            Category.TAG_RULE,
                            severity,
                            f"{rule.describe()}: {hours(total)} in {ctx.period.week_label(week)}",
                            minutes=rule.minutes - total,
                        )
                    )
        elif rule.kind == TagRuleKind.NOT_ALONE:
            for day in ctx.period.days:
                alone: list[int] = []
                for slot, emps in sorted(duty[day].items()):
                    tagged = [e for e in emps if (p := ctx.people.get(e)) and p.has_tag(rule.tag, day)]
                    if tagged and len(tagged) == len(emps):
                        alone.append(slot)
                for iv in runs(alone):
                    issues.append(
                        Issue(
                            Category.TAG_RULE,
                            severity,
                            f"{rule.describe()}: only {rule.tag} staff on duty {day_label(day)} {iv.label()}",
                            day,
                            iv,
                            minutes=iv.minutes,
                        )
                    )
        elif rule.kind == TagRuleKind.NOT_SOLO:
            for day in ctx.period.days:
                solo = [
                    slot
                    for slot, emps in sorted(duty[day].items())
                    if len(emps) == 1 and (p := ctx.people.get(emps[0])) and p.has_tag(rule.tag, day)
                ]
                for iv in runs(solo):
                    issues.append(
                        Issue(
                            Category.TAG_RULE,
                            severity,
                            f"{rule.describe()}: alone on duty {day_label(day)} {iv.label()}",
                            day,
                            iv,
                            minutes=iv.minutes,
                        )
                    )
    return issues


# --- 5. Duties ------------------------------------------------------------------------------


def check_duties(ctx: ScheduleContext) -> list[Issue]:
    """Opener duties (scenario 3) and week-hours duties (scenario 4)."""
    issues: list[Issue] = []
    for o in ctx.rules.openers:
        who = name(ctx, o.employee_id)
        for day in ctx.period.days:
            open_ivs = ctx.open_on(day)
            if day.weekday() not in o.weekdays or not open_ivs:
                continue
            opening = open_ivs[0].start
            if not any(
                s.employee_id == o.employee_id and s.day == day and s.interval.start == opening
                for s in ctx.shifts
            ):
                issues.append(
                    Issue(
                        Category.DUTY,
                        Severity.WARNING,
                        f"{who} has no opening shift on {day_label(day)}",
                        day,
                        employee_id=o.employee_id,
                    )
                )
    per_week = minutes_by_person_week(ctx.shifts, ctx.period)
    for d in ctx.rules.week_duties:
        who = name(ctx, d.employee_id)
        for week in range(ctx.period.weeks):
            used = per_week.get(d.employee_id, {}).get(week, 0)
            if week in d.weeks and used < d.minutes:
                issues.append(
                    Issue(
                        Category.DUTY,
                        Severity.WARNING,
                        f"{who}: {hours(used)} in their {d.label} ({ctx.period.week_label(week)}), "
                        f"{hours(d.minutes)} expected",
                        employee_id=d.employee_id,
                        minutes=d.minutes - used,
                    )
                )
            elif week not in d.weeks and used > d.off_week_max:
                issues.append(
                    Issue(
                        Category.DUTY,
                        Severity.ERROR,
                        f"{who}: {hours(used)} in {ctx.period.week_label(week)}, over the {hours(d.off_week_max)} "
                        f"allowed outside their {d.label}",
                        employee_id=d.employee_id,
                        minutes=used - d.off_week_max,
                    )
                )
    return issues


# --- 8. Drift and open shifts ---------------------------------------------------------------


def check_drift(ctx: ScheduleContext) -> list[Issue]:
    issues: list[Issue] = []
    for s in ctx.shifts:
        if s.employee_id is None:
            issues.append(
                Issue(
                    Category.OPEN_SHIFT,
                    Severity.INFO,
                    f"Open shift {day_label(s.day)} {s.interval.label()} has no one assigned",
                    s.day,
                    s.interval,
                    shift_id=s.id,
                )
            )
    if ctx.published is None:
        return issues
    published = {s.id: s for s in ctx.published}
    current = {s.id: s for s in ctx.shifts}
    for sid, s in current.items():
        old = published.get(sid)
        if old is None:
            issues.append(
                Issue(
                    Category.DRIFT,
                    Severity.INFO,
                    f"New: {name(ctx, s.employee_id)} {day_label(s.day)} {s.interval.label()}",
                    s.day,
                    s.interval,
                    s.employee_id,
                    sid,
                )
            )
        elif not old.same_assignment(s):
            issues.append(
                Issue(
                    Category.DRIFT,
                    Severity.INFO,
                    f"Changed: {name(ctx, old.employee_id)} {day_label(old.day)} {old.interval.label()} "
                    f"→ {name(ctx, s.employee_id)} {day_label(s.day)} {s.interval.label()}",
                    s.day,
                    s.interval,
                    s.employee_id,
                    sid,
                )
            )
    for sid, old in published.items():
        if sid not in current:
            issues.append(
                Issue(
                    Category.DRIFT,
                    Severity.INFO,
                    f"Removed: {name(ctx, old.employee_id)} {day_label(old.day)} {old.interval.label()}",
                    old.day,
                    old.interval,
                    old.employee_id,
                    sid,
                )
            )
    return issues


CHECKS = (
    check_coverage,
    check_availability,
    check_shape,
    check_caps,
    check_tag_rules,
    check_duties,
    check_drift,
)


def validate(
    ctx: ScheduleContext, checks: Iterable[Callable[[ScheduleContext], list[Issue]]] = CHECKS
) -> Report:
    report = Report()
    for check in checks:
        report.issues.extend(check(ctx))
    return report
