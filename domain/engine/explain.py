# SPDX-License-Identifier: AGPL-3.0-or-later
"""Explanations (SPEC §7.4, CLAUDE.md §2.5): why someone has few hours, why a target wasn't met,
which rule forced a trade-off, and what could be tried next (D36)."""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from domain.engine.candidates import Scope
from domain.rules import CapScope
from domain.schedule import ScheduleContext, Shift, minutes_by_person_week
from domain.timeutil import format_minute_12h, intersect, total_minutes
from domain.validation import Category, Issue, Report, day_label


def hours(minutes: int) -> str:
    return f"{minutes / 60:g}"


@dataclass(frozen=True)
class Unmet:
    """A rule left unmet, why, and the what-ifs that might help (D36)."""

    category: Category
    message: str
    reason: str
    what_ifs: tuple[str, ...] = ()


@dataclass
class Explanation:
    people: dict[int, str] = field(default_factory=lambda: {})
    trade_offs: list[str] = field(default_factory=lambda: [])
    unmet: list[Unmet] = field(default_factory=lambda: [])


WHAT_IF_LABELS = {
    "allow_published": "Allow changes to published shifts for this run",
    "shrink_freeze": "Shrink the freeze window for this run",
    "relax_rule": "Relax this rule for this period",
}


def available_minutes(ctx: ScheduleContext, employee_id: int, days: Sequence[date]) -> int:
    person = ctx.people[employee_id]
    return sum(total_minutes(person.availability.available_within(d.weekday(), ctx.open_on(d))) for d in days)


def explain(
    ctx: ScheduleContext, shifts: Sequence[Shift], report: Report, scope: Scope, proven: bool = True
) -> Explanation:
    """`proven` says whether the schedule is proven optimal; if not, trade-offs aren't called forced."""
    out = Explanation()
    per_week = minutes_by_person_week(shifts, ctx.period)
    open_total = sum(total_minutes(ctx.open_on(d)) for d in ctx.period.days)
    working = [sum(w.values()) for w in per_week.values() if sum(w.values()) > 0]
    median = statistics.median(working) if working else 0

    # Low hours (SPEC §7.4): limited availability, or balancing.
    for pid, person in ctx.people.items():
        if not person.auto_schedulable:
            continue
        total = sum(per_week.get(pid, {}).values())
        if not person.availability.is_known:
            if total:
                out.people[pid] = (
                    f"Scheduled for {hours(total)}h although availability isn't entered: no one else could cover "
                    "those times. Ask them to enter availability."
                )
            continue
        if median and total < median / 2:
            avail = available_minutes(ctx, pid, ctx.period.days)
            if avail < open_total * 0.4:
                out.people[pid] = (
                    f"Low hours because available only {hours(avail)} of {hours(open_total)} open hours."
                )
            else:
                out.people[pid] = (
                    f"Low hours ({hours(total)}h) because shifts went to coverage and higher-priority goals while "
                    f"hours were balanced; available for {hours(avail)} of {hours(open_total)} open hours."
                )

    # Unmet hour targets: which cap or availability limit blocked them.
    for t in ctx.rules.targets:
        person = ctx.people.get(t.employee_id)
        if person is None:
            continue
        for week in range(ctx.period.weeks):
            if t.weeks is not None and week not in t.weeks:
                continue
            got = per_week.get(t.employee_id, {}).get(week, 0)
            if got >= t.minutes:
                continue
            days = ctx.period.week_days(week)
            avail = available_minutes(ctx, t.employee_id, days)
            caps = [
                c
                for c in ctx.rules.caps
                if c.scope == CapScope.WEEK
                and (c.employee_id in (None, t.employee_id))
                and (c.tag is None or person.tagged_in(c.tag, days))
            ]
            binding = [c for c in caps if c.minutes <= got]
            if binding:
                reason = f"capped at {hours(binding[0].minutes)}h by the {binding[0].label or binding[0].scope.value + ' cap'}"
            elif avail < t.minutes:
                reason = f"available for only {hours(avail)}h of open hours that week"
            else:
                reason = (
                    "every extra hour would have meant over-coverage or a gap, or taking hours from someone with a "
                    "higher-priority goal"
                )
            msg = f"{person.name}: {hours(got)}h of the {hours(t.minutes)}h target in {ctx.period.week_label(week)}"
            out.unmet.append(Unmet(Category.CAP, msg, reason, ("relax_rule",)))

    # Forced trade-offs: over-coverage the shape rules made unavoidable (CLAUDE.md §4).
    for issue in report.issues:
        if issue.category == Category.OVER_COVERAGE and issue.day is not None and issue.interval is not None:
            out.trade_offs.append(_explain_over(ctx, shifts, issue, proven))
        elif issue.category == Category.COVERAGE_GAP and issue.day is not None and issue.interval is not None:
            out.trade_offs.append(_explain_gap(ctx, issue))

    # Unmet rules with what-ifs (D36).
    what_ifs: list[str] = []
    if scope.locked_ids:
        what_ifs.append("allow_published")
    if scope.freeze_until is not None:
        what_ifs.append("shrink_freeze")
    for issue in report.counted:
        if issue.category in (Category.COVERAGE_GAP, Category.OVER_COVERAGE, Category.STATUS):
            continue  # explained as trade-offs above
        reason = {
            Category.TAG_RULE: _tag_reason(ctx, issue),
            Category.CAP: "a locked or out-of-scope shift already uses the hours",
            Category.AVAILABILITY: "a locked or out-of-scope shift clashes with availability",
            Category.SHAPE: "a locked or out-of-scope shift breaks the shape rules",
            Category.DOUBLE_BOOKING: "two locked shifts overlap",
            Category.CLOSED: "a locked shift runs while the location is closed",
            Category.DUTY: _duty_reason(ctx, issue, proven),
        }.get(issue.category, "")
        extra = ("relax_rule",) if issue.category == Category.TAG_RULE else ()
        out.unmet.append(Unmet(issue.category, issue.message, reason, tuple(what_ifs) + extra))
    return out


def _tag_reason(ctx: ScheduleContext, issue: Issue) -> str:
    """Whether anyone with the tag was free at all, from the rule named in the message."""
    if issue.day is None or issue.interval is None:
        return "not enough hours from people with the tag fit around caps and other rules"
    rule = next((r for r in ctx.rules.tag_rules if issue.message.startswith(r.describe())), None)
    if rule is None:
        return "not enough people with the tag were free at those times"
    day = issue.day
    free = [
        p
        for p in ctx.people.values()
        if p.auto_schedulable
        and p.has_tag(rule.tag, day)
        and p.availability.is_known
        and intersect([issue.interval], p.availability.available_within(day.weekday(), ctx.open_on(day)))
    ]
    if not free:
        return f"no one with the {rule.tag} tag is available at that time"
    return f"{len(free)} {rule.tag} {'person is' if len(free) == 1 else 'people are'} free then, but caps and other rules used their hours elsewhere"


def _duty_reason(ctx: ScheduleContext, issue: Issue, proven: bool) -> str:
    person = ctx.people.get(issue.employee_id) if issue.employee_id is not None else None
    if person is None:
        return "the duty couldn't be met"
    if issue.day is not None:  # an opening shift
        open_ivs = ctx.open_on(issue.day)
        free = person.availability.available_within(issue.day.weekday(), open_ivs)
        if not open_ivs or not free or free[0].start != open_ivs[0].start:
            return f"{person.name} isn't available at opening time that day"
    if not proven:
        return "the solver stopped at its time limit before fitting it in; a longer run may"
    return "a cap or a higher-priority rule left no room for it"


def _explain_over(ctx: ScheduleContext, shifts: Sequence[Shift], issue: Issue, proven: bool = True) -> str:
    if issue.day is None or issue.interval is None:
        return issue.message
    day, run = issue.day, issue.interval
    on = [s for s in shifts if s.day == day and s.employee_id is not None and s.interval.overlaps(run)]
    min_len = ctx.rules.shape.min_shift
    extra = issue.minutes // max(run.minutes, 1)
    head = f"{day_label(day)} {run.label()} has {extra} more on duty than needed"
    facts = [f"every shift must be at least {hours(min_len)}h"]
    for o in ctx.rules.openers:
        for s in on:
            if s.employee_id == o.employee_id and day.weekday() in o.short_weekdays:
                person = ctx.people.get(s.employee_id) if s.employee_id is not None else None
                name = person.name if person else "the opener"
                facts.insert(0, f"{name} opens and leaves at {format_minute_12h(s.interval.end)}")
    open_ivs = ctx.open_on(day)
    containing = next((iv for iv in open_ivs if iv.start <= run.start < iv.end), None)
    if containing is not None:
        closure = next(
            (c for c in ctx.closures if c.applies_on(day) and c.span.start == containing.end), None
        )
        if closure is not None:
            facts.append(f"the location closes at {format_minute_12h(containing.end)} for {closure.reason}")
        elif containing.end - run.end < min_len:
            facts.append(f"the location closes at {format_minute_12h(containing.end)}")
    if not proven and len(facts) == 1:
        # Nothing specific forces it, and the solver stopped at its time limit: don't call it forced.
        return f"{head}. The solver stopped at its time limit, so a longer run may remove this."
    return f"{head} because {_join(facts)}. The alternative is a shift shorter than the minimum, or a gap."


def _explain_gap(ctx: ScheduleContext, issue: Issue) -> str:
    if issue.day is None or issue.interval is None:
        return issue.message
    day, run = issue.day, issue.interval
    free = [
        p.name
        for p in ctx.people.values()
        if p.auto_schedulable
        and p.availability.is_known
        and intersect([run], p.availability.available_within(day.weekday(), ctx.open_on(day))) == [run]
    ]
    if not free:
        return f"{day_label(day)} {run.label()} is short: no one with entered availability is free for that time."
    return (
        f"{day_label(day)} {run.label()} is short: {len(free)} people are free then, but each was held back by a cap, "
        "a shape rule or a duty."
    )


def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
