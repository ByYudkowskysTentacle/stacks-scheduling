# SPDX-License-Identifier: AGPL-3.0-or-later
"""Suggestion queries (SPEC §7.5) and remove-from-pool reassignment (SPEC §7.3, scenario 9).

Every query tries the change on a copy of the schedule and runs the person-level checks of the
validation suite (availability, shape and double booking, caps, tag rules, duties), plus open
hours. Coverage is left out on purpose: a gap that shrinks would read as a new issue. Someone is
eligible exactly when the suite reports nothing new at error level, so a suggestion never
contradicts what the suite says after it is applied. New warnings, such as "hasn't entered
availability", are shown next to the person rather than hiding them.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from itertools import combinations

from domain.availability import AvailabilityStatus
from domain.rules import RotationMode
from domain.schedule import Person, ScheduleContext, Shift, minutes_by_person_week
from domain.timeutil import SLOT_MINUTES, Interval, intersect, subtract, total_minutes
from domain.validation import (
    Issue,
    Severity,
    check_availability,
    check_caps,
    check_duties,
    check_shape,
    check_tag_rules,
    day_label,
    hours,
)

MOVE_CHECKS: tuple[Callable[[ScheduleContext], list[Issue]], ...] = (
    check_availability,
    check_shape,
    check_caps,
    check_tag_rules,
    check_duties,
)

# --- Shared pieces --------------------------------------------------------------------------


def _issues(ctx: ScheduleContext, shifts: Sequence[Shift]) -> list[Issue]:
    trial = replace(ctx, shifts=list(shifts))
    return [i for check in MOVE_CHECKS for i in check(trial)]


def _new(before: Iterable[Issue], after: Iterable[Issue]) -> list[Issue]:
    """Issues in `after` that weren't in `before`. Messages carry the numbers, so a cap breach
    that gets worse counts as new."""
    seen = Counter((i.category, i.message) for i in before)
    out: list[Issue] = []
    for i in after:
        key = (i.category, i.message)
        if seen[key]:
            seen[key] -= 1
        else:
            out.append(i)
    return out


def _preferred(person: Person, day: date, interval: Interval) -> bool:
    """The whole shift falls in the person's preferred time."""
    pref = person.availability.preferred_for_scheduling(day.weekday())
    return total_minutes(intersect([interval], pref)) == interval.minutes


def _rotation_note(ctx: ScheduleContext, person: Person, day: date) -> tuple[str, Severity] | None:
    rotation = ctx.rules.rotation
    week = ctx.period.week_of(day)
    if rotation.on_week(person.id, week):
        return None
    group = rotation.members.get(person.id, "")
    text = f"{person.name} is in rotation group {group}, which is off in {ctx.period.week_label(week)}"
    return (text, Severity.ERROR if rotation.mode == RotationMode.STRICT else Severity.WARNING)


def _week_minutes(ctx: ScheduleContext, shifts: Sequence[Shift], employee_id: int, day: date) -> int:
    return minutes_by_person_week(shifts, ctx.period).get(employee_id, {}).get(ctx.period.week_of(day), 0)


def _period_minutes(ctx: ScheduleContext, shifts: Sequence[Shift], employee_id: int) -> int:
    return sum(minutes_by_person_week(shifts, ctx.period).get(employee_id, {}).values())


# --- Who could take this shift? / Who could cover this gap? ---------------------------------


@dataclass(frozen=True)
class Taker:
    employee_id: int
    name: str
    week_minutes: int  # in the shift's week, before taking it
    period_minutes: int
    preferred: bool
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def eligible(self) -> bool:
        return not self.blockers


@dataclass(frozen=True)
class TakerResult:
    day: date
    interval: Interval
    holder_id: int | None
    takers: list[Taker]
    holder_effects: tuple[str, ...] = ()  # what giving the shift away does to its holder

    @property
    def eligible(self) -> list[Taker]:
        return [t for t in self.takers if t.eligible]

    @property
    def blocked(self) -> list[Taker]:
        return [t for t in self.takers if not t.eligible]


def _rank(t: Taker) -> tuple[bool, int, bool, int, int, str]:
    """Eligible first, then fewest new warnings (so someone who hasn't entered availability, or
    whose shift would miss a soft tag rule, comes after a clean match), then preferred time,
    then fewest hours that week and period (SPEC §7.3)."""
    return (
        not t.eligible,
        len(t.warnings),
        not t.preferred,
        t.week_minutes,
        t.period_minutes,
        t.name.lower(),
    )


def _try(
    ctx: ScheduleContext,
    shifts: Sequence[Shift],
    before: list[Issue],
    target: Shift,
    person: Person,
    ignore: int | None,
) -> Taker:
    """Give `target` (already in `shifts`, or new) to `person` and report what the suite says."""
    blockers: list[str] = []
    warnings: list[str] = []
    if not person.auto_schedulable:
        blockers.append(f"{person.name} isn't in the scheduling pool")
    rot = _rotation_note(ctx, person, target.day)
    if rot is not None:
        (blockers if rot[1] == Severity.ERROR else warnings).append(rot[0])
    closed = subtract([target.interval], ctx.open_on(target.day))
    if closed:
        blockers.append(
            f"the library is closed {day_label(target.day)} {', '.join(iv.label() for iv in closed)}"
        )
    moved = replace(target, employee_id=person.id)
    trial = [moved if s is target else s for s in shifts] if target in shifts else [*shifts, moved]
    for issue in _new(before, _issues(ctx, trial)):
        if ignore is not None and issue.employee_id == ignore:
            continue  # the holder's side, reported once in holder_effects
        if issue.severity == Severity.ERROR:
            blockers.append(issue.message)
        elif issue.severity == Severity.WARNING:
            warnings.append(issue.message)
    return Taker(
        employee_id=person.id,
        name=person.name,
        week_minutes=_week_minutes(ctx, shifts, person.id, target.day),
        period_minutes=_period_minutes(ctx, shifts, person.id),
        preferred=person.availability.status != AvailabilityStatus.NOT_ENTERED
        and _preferred(person, target.day, target.interval),
        blockers=tuple(dict.fromkeys(blockers)),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def holder_effects(ctx: ScheduleContext, shift: Shift) -> tuple[str, ...]:
    """What the shift's holder loses if it goes to someone else: a duty it fulfils, or the
    hours that keep them at a target (scenario 13 calls these shifts protected)."""
    if shift.employee_id is None:
        return ()
    shifts = list(ctx.shifts)
    without = [s for s in shifts if s is not shift]
    out = [
        i.message
        for i in _new(_issues(ctx, shifts), _issues(ctx, without))
        if i.employee_id == shift.employee_id and i.severity != Severity.INFO
    ]
    who = ctx.people[shift.employee_id].name if shift.employee_id in ctx.people else "The holder"
    week = ctx.period.week_of(shift.day)
    for t in ctx.rules.targets:
        if t.employee_id != shift.employee_id or (t.weeks is not None and week not in t.weeks):
            continue
        after = _week_minutes(ctx, without, t.employee_id, shift.day)
        if after < t.minutes:
            out.append(f"{who} would drop to {hours(after)}, below their target of {hours(t.minutes)}")
    return tuple(dict.fromkeys(out))


def who_could_take(ctx: ScheduleContext, shift: Shift) -> TakerResult:
    """Everyone who could take `shift` (an existing shift, or a new one for a gap), eligible
    first, and the rule that stops each of the others."""
    shifts = list(ctx.shifts)
    before = _issues(ctx, shifts)
    holder = shift.employee_id
    takers = [
        _try(ctx, shifts, before, shift, person, holder)
        for person in ctx.people.values()
        if person.id != holder
    ]
    return TakerResult(
        day=shift.day,
        interval=shift.interval,
        holder_id=holder,
        takers=sorted(takers, key=_rank),
        holder_effects=holder_effects(ctx, shift) if shift in shifts else (),
    )


def gap_shift(day: date, interval: Interval, location_id: int | None = None) -> Shift:
    """A new, unsaved shift for "who could cover this gap?"."""
    return Shift(None, None, day, interval, location_id=location_id)


def widen_gap(ctx: ScheduleContext, day: date, interval: Interval) -> Interval:
    """A gap shorter than the minimum shift, widened to the minimum inside the same open
    interval: later first, then earlier. Otherwise every candidate would fail the shape check."""
    need = ctx.rules.shape.min_shift
    if interval.minutes >= need:
        return interval
    around = next((iv for iv in ctx.open_on(day) if iv.contains(interval)), None)
    if around is None:
        return interval
    end = min(around.end, interval.start + need)
    return Interval(max(around.start, end - need), end)


# --- How can I raise this person's hours? ---------------------------------------------------


@dataclass(frozen=True)
class Opportunity:
    shift: Shift
    holder_id: int
    holder_name: str
    holder_week_minutes: int
    holder_period_minutes: int
    holder_is_low: bool
    preferred: bool
    warnings: tuple[str, ...] = ()


@dataclass
class OpportunityResult:
    employee_id: int
    name: str
    week_minutes: list[int]
    by_week: dict[int, list[Opportunity]] = field(default_factory=lambda: {})
    protected: int = 0  # takeable shifts left out because a duty or target protects them
    low_threshold: dict[int, int] = field(default_factory=lambda: {})


def _low_threshold(ctx: ScheduleContext, week: int) -> int:
    """Median weekly hours among pool members who work that week: at or below it counts as low."""
    per = minutes_by_person_week(ctx.shifts, ctx.period)
    values = [
        per[p.id][week]
        for p in ctx.people.values()
        if p.auto_schedulable and per.get(p.id, {}).get(week, 0) > 0
    ]
    return int(statistics.median(values)) if values else 0


def _quick_no(ctx: ScheduleContext, person: Person, shifts: Sequence[Shift], s: Shift) -> bool:
    """Cheap filters before running the suite: unavailable, already working then, strict rotation."""
    if intersect([s.interval], person.blocked_on(s.day)):
        return True
    if any(o.employee_id == person.id and o.day == s.day and o.interval.overlaps(s.interval) for o in shifts):
        return True
    rot = _rotation_note(ctx, person, s.day)
    return rot is not None and rot[1] == Severity.ERROR


def opportunities(ctx: ScheduleContext, employee_id: int) -> OpportunityResult:
    """Shifts `employee_id` could take over, grouped by week, with each holder's hours and
    whether the holder is low themselves (SPEC §7.5, scenario 13). Locked shifts and shifts a
    duty or target protects are left out."""
    person = ctx.people[employee_id]
    shifts = list(ctx.shifts)
    before = _issues(ctx, shifts)
    per = minutes_by_person_week(shifts, ctx.period)
    thresholds = {w: _low_threshold(ctx, w) for w in range(ctx.period.weeks)}
    targets = {t.employee_id: t.minutes for t in ctx.rules.targets}
    desired = {d.employee_id: d.minutes for d in ctx.rules.desired}
    result = OpportunityResult(
        employee_id=employee_id,
        name=person.name,
        week_minutes=[per.get(employee_id, {}).get(w, 0) for w in range(ctx.period.weeks)],
        low_threshold=thresholds,
    )
    for s in sorted(shifts, key=lambda s: (s.day, s.interval)):
        if s.employee_id in (None, employee_id) or s.locked or _quick_no(ctx, person, shifts, s):
            continue
        taker = _try(ctx, shifts, before, s, person, s.employee_id)
        if not taker.eligible:
            continue
        if holder_effects(ctx, s):
            result.protected += 1
            continue
        holder = s.employee_id
        week = ctx.period.week_of(s.day)
        holder_week = per.get(holder, {}).get(week, 0)
        # Low: at or below the median that week already, or giving this up would take them
        # below their own target or approved desired hours.
        own_floor = max(targets.get(holder, 0), desired.get(holder, 0))
        result.by_week.setdefault(week, []).append(
            Opportunity(
                shift=s,
                holder_id=holder,
                holder_name=ctx.people[holder].name if holder in ctx.people else f"Employee #{holder}",
                holder_week_minutes=holder_week,
                holder_period_minutes=sum(per.get(holder, {}).values()),
                holder_is_low=holder_week <= thresholds[week] or holder_week - s.minutes < own_floor,
                preferred=taker.preferred,
                warnings=taker.warnings,
            )
        )
    for items in result.by_week.values():
        # Shifts from holders with hours to spare first, then the busiest holders.
        items.sort(key=lambda o: (o.holder_is_low, -o.holder_week_minutes, o.shift.day, o.shift.interval))
    return result


# --- Remove from pool -----------------------------------------------------------------------


@dataclass(frozen=True)
class Reassignment:
    shift: Shift
    to_id: int | None  # None: no one eligible, so it becomes an open shift
    to_name: str
    reason: str


def remove_from_pool_plan(ctx: ScheduleContext, employee_id: int, from_day: date) -> list[Reassignment]:
    """Reassign the person's shifts from `from_day` on to eligible others at the same times,
    one at a time in date order so each choice sees the ones before it. Prefers preferred-time
    matches, then the fewest hours. A shift nobody can take becomes an open shift: nothing is
    deleted (SPEC §9.4, scenario 9)."""
    leaving = ctx.people.get(employee_id)
    people = dict(ctx.people)
    if leaving is not None:
        people[employee_id] = replace(leaving, auto_schedulable=False)
    work = replace(ctx, people=people)
    shifts = list(ctx.shifts)
    plan: list[Reassignment] = []
    mine = sorted(
        (s for s in shifts if s.employee_id == employee_id and s.day >= from_day and not s.locked),
        key=lambda s: (s.day, s.interval),
    )
    for s in mine:
        current = s  # only this loop replaces the person's own shifts, in order
        work = replace(work, shifts=shifts)
        before = _issues(work, shifts)
        candidates = [
            _try(work, shifts, before, current, p, employee_id)
            for p in people.values()
            if p.id != employee_id and p.auto_schedulable and not _quick_no(work, p, shifts, current)
        ]
        best = min((t for t in candidates if t.eligible), key=_rank, default=None)
        if best is None:
            plan.append(
                Reassignment(current, None, "Open shift", "no one eligible is free; left as an open shift")
            )
            new = replace(current, employee_id=None)
        else:
            why = [f"{hours(best.week_minutes)} that week"]
            if best.preferred:
                why.insert(0, "preferred time")
            if best.warnings:
                why.append("; ".join(best.warnings))
            plan.append(Reassignment(current, best.employee_id, best.name, ", ".join(why)))
            new = replace(current, employee_id=best.employee_id)
        shifts = [new if x is current else x for x in shifts]
    return plan


def describe(r: Reassignment) -> str:
    return f"{day_label(r.shift.day)} {r.shift.interval.label()} → {r.to_name} ({r.reason})"


# --- Checking a whole change ----------------------------------------------------------------


@dataclass(frozen=True)
class ChangeCheck:
    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


def check_change(ctx: ScheduleContext, after: Sequence[Shift], pool_rule: bool = True) -> ChangeCheck:
    """What a change adds to the person-level checks, plus shifts that run while closed.
    `pool_rule=False` lets people outside the scheduling pool take shifts: the pool limits only
    the generator and suggestions, not a supervisor approving someone's own request (D104)."""
    errors: list[str] = []
    warnings: list[str] = []
    before_ids = {id(s) for s in ctx.shifts}
    originals = {s.id: s for s in ctx.shifts if s.id is not None}
    for s in after:
        old = originals.get(s.id) if s.id is not None else None
        if id(s) in before_ids or (
            old is not None and old.employee_id == s.employee_id and old.interval.contains(s.interval)
        ):
            continue  # unchanged, or the same person keeping part of their own shift
        closed = subtract([s.interval], ctx.open_on(s.day))
        if closed:
            errors.append(
                f"the library is closed {day_label(s.day)} {', '.join(iv.label() for iv in closed)}"
            )
        person = ctx.people.get(s.employee_id) if s.employee_id is not None else None
        if person is not None:
            if pool_rule and not person.auto_schedulable:
                errors.append(f"{person.name} isn't in the scheduling pool")
            rot = _rotation_note(ctx, person, s.day)
            if rot is not None:
                (errors if rot[1] == Severity.ERROR else warnings).append(rot[0])
    for i in _new(_issues(ctx, ctx.shifts), _issues(ctx, after)):
        if i.severity == Severity.ERROR:
            errors.append(i.message)
        elif i.severity == Severity.WARNING:
            warnings.append(i.message)
    return ChangeCheck(tuple(dict.fromkeys(errors)), tuple(dict.fromkeys(warnings)))


# --- Targeted move (scenario 10) ------------------------------------------------------------


@dataclass(frozen=True)
class MoveOption:
    shift: Shift
    donor_id: int
    donor_name: str
    preferred: bool
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class SplitOption:
    """Part of a donor's shift goes to the recipient; the donor keeps the rest on the original
    shift, so its ID and history survive (D102)."""

    shift: Shift  # the donor's shift as it is now
    donor_id: int
    donor_name: str
    part: Interval  # what the recipient takes
    keep: Interval  # what the donor keeps
    preferred: bool
    warnings: tuple[str, ...] = ()


@dataclass
class TargetedMove:
    recipient_id: int
    recipient_name: str
    wanted_minutes: int
    chosen: list[MoveOption]
    feasible: list[MoveOption]
    notes: list[str]
    check: ChangeCheck | None = None
    splits: list[SplitOption] = field(default_factory=list[SplitOption])

    @property
    def chosen_minutes(self) -> int:
        return sum(o.shift.minutes for o in self.chosen)


MAX_SHIFTS_PER_MOVE = 3
CLOSE_ENOUGH = 30  # minutes: a whole-shift set this close to the ask needs no split (D102)
MAX_SPLITS = 3
SPLIT_LENGTHS_TRIED = 8  # per shift, the part lengths closest to the ask


def targeted_move(
    ctx: ScheduleContext,
    recipient_id: int,
    donor_ids: Sequence[int],
    minutes: int,
    weeks: frozenset[int] | None = None,
) -> TargetedMove:
    """ "Move about 2 hours from D, T or P to J, keeping J in one week" (SPEC §7.3, scenario 10).
    Picks the set of whole shifts closest to the hours asked for (fewest shifts on a tie), and
    says for each donor why they couldn't give. When no whole-shift set comes within
    CLOSE_ENOUGH, it also offers splits: the start or end of one donor shift (D102)."""
    recipient = ctx.people[recipient_id]
    shifts = list(ctx.shifts)
    before = _issues(ctx, shifts)
    feasible: list[MoveOption] = []
    splittable: list[tuple[Shift, int, str]] = []
    notes: list[str] = []
    week_text = " and ".join(ctx.period.week_label(w) for w in sorted(weeks)) if weeks is not None else ""
    for donor_id in donor_ids:
        donor = ctx.people.get(donor_id)
        dname = donor.name if donor else f"Employee #{donor_id}"
        theirs = sorted(
            (s for s in shifts if s.employee_id == donor_id and not s.locked),
            key=lambda s: (s.day, s.interval),
        )
        if not theirs:
            notes.append(f"{dname} has no shifts that can move in this period.")
            continue
        out_of_week: list[Shift] = []
        protected: list[tuple[Shift, str]] = []
        blocked: list[tuple[Shift, tuple[str, ...]]] = []
        mine: list[MoveOption] = []
        for s in theirs:
            if weeks is not None and ctx.period.week_of(s.day) not in weeks:
                out_of_week.append(s)
                continue
            effects = holder_effects(ctx, s)
            if effects:
                protected.append((s, effects[0]))
                continue
            splittable.append((s, donor_id, dname))
            taker = _try(ctx, shifts, before, s, recipient, donor_id)
            if taker.eligible:
                mine.append(MoveOption(s, donor_id, dname, taker.preferred, taker.warnings))
            else:
                blocked.append((s, taker.blockers))
        feasible.extend(mine)
        if mine:
            listed = ", ".join(f"{day_label(o.shift.day)} {o.shift.interval.label()}" for o in mine)
            notes.append(f"{dname}: {len(mine)} of {len(theirs)} shifts could move ({listed}).")
        elif len(out_of_week) == len(theirs):
            notes.append(f"All of {dname}'s shifts are outside {week_text}, where {recipient.name} is kept.")
        elif blocked and all(any("unavailable" in b for b in bs) for _, bs in blocked) and not protected:
            extra = f"; the rest are outside {week_text}" if out_of_week else ""
            notes.append(f"None of {dname}'s shifts fit {recipient.name}'s availability{extra}.")
        else:
            parts = [f"{day_label(s.day)} {s.interval.label()}: {bs[0]}" for s, bs in blocked]
            parts += [f"{day_label(s.day)} {s.interval.label()}: protected ({why})" for s, why in protected]
            if out_of_week:
                parts.append(f"{len(out_of_week)} outside {week_text}")
            notes.append(f"None of {dname}'s shifts can move. " + "; ".join(parts) + ".")
    chosen, check = _best_subset(ctx, feasible, recipient_id, minutes)
    if feasible and not chosen:
        notes.append("No combination of the movable shifts passes every rule together.")
    splits: list[SplitOption] = []
    gap = abs(sum(o.shift.minutes for o in chosen) - minutes) if chosen else None
    if gap is None or gap > CLOSE_ENOUGH:
        splits = _split_options(ctx, splittable, recipient, minutes, gap)
        if splits:
            notes.append(
                "No set of whole shifts comes close to the hours asked for, so splits are offered too."
            )
    return TargetedMove(recipient_id, recipient.name, minutes, chosen, feasible, notes, check, splits)


def split_off(
    ctx: ScheduleContext, shift: Shift, part: Interval, recipient_id: int
) -> tuple[list[Shift], ChangeCheck]:
    """The schedule after `part` (the start or the end of `shift`) goes to the recipient and the
    holder keeps the rest on the original shift, and what the checks say."""
    iv = shift.interval
    if part.start == iv.start and part.end < iv.end:
        keep = Interval(part.end, iv.end)
    elif part.end == iv.end and part.start > iv.start:
        keep = Interval(iv.start, part.start)
    else:
        raise ValueError("the part must be the start or the end of the shift")
    new = Shift(None, recipient_id, shift.day, part, shift.pay_role_id, shift.location_id)
    after = [replace(s, interval=keep) if s is shift else s for s in ctx.shifts] + [new]
    return after, check_change(ctx, after)


def _split_options(
    ctx: ScheduleContext,
    shifts: list[tuple[Shift, int, str]],
    recipient: Person,
    minutes: int,
    whole_gap: int | None,
) -> list[SplitOption]:
    """For each shift, the part closest to `minutes` (from its start or its end) that passes
    every check, if it beats the best whole-shift set. Best few first."""
    step = SLOT_MINUTES
    found: list[tuple[int, SplitOption]] = []
    for s, donor_id, dname in shifts:
        lengths = sorted(range(step, s.minutes, step), key=lambda n: (abs(n - minutes), -n))
        for n in lengths[:SPLIT_LENGTHS_TRIED]:
            if whole_gap is not None and abs(n - minutes) >= whole_gap:
                break
            option = None
            for part in (
                Interval(s.interval.start, s.interval.start + n),
                Interval(s.interval.end - n, s.interval.end),
            ):
                _, check = split_off(ctx, s, part, recipient.id)
                if check.ok:
                    keep = subtract([s.interval], [part])[0]
                    option = SplitOption(
                        s, donor_id, dname, part, keep, _preferred(recipient, s.day, part), check.warnings
                    )
                    break
            if option is not None:
                found.append((abs(n - minutes), option))
                break
    found.sort(key=lambda t: (t[0], not t[1].preferred, len(t[1].warnings), t[1].shift.day))
    return [o for _, o in found[:MAX_SPLITS]]


def _best_subset(
    ctx: ScheduleContext, options: list[MoveOption], recipient_id: int, minutes: int
) -> tuple[list[MoveOption], ChangeCheck | None]:
    """The set of up to three shifts closest to `minutes` that passes the checks together."""
    subsets: list[tuple[MoveOption, ...]] = []
    for k in range(1, min(MAX_SHIFTS_PER_MOVE, len(options)) + 1):
        subsets.extend(combinations(options, k))
    subsets.sort(
        key=lambda c: (
            abs(sum(o.shift.minutes for o in c) - minutes),
            len(c),
            -sum(o.preferred for o in c),
            sum(len(o.warnings) for o in c),
        )
    )
    for combo in subsets[:200]:
        moving = {id(o.shift) for o in combo}
        after = [replace(s, employee_id=recipient_id) if id(s) in moving else s for s in ctx.shifts]
        check = check_change(ctx, after)
        if check.ok:
            return list(combo), check
    return [], None


# --- Split shift (scenario 11) --------------------------------------------------------------


@dataclass(frozen=True)
class SplitPlan:
    first: Shift  # the original shift, retimed (keeps its ID and history)
    second: Shift  # new
    first_takers: TakerResult
    second_takers: TakerResult


def split_shift(ctx: ScheduleContext, shift: Shift, at: int) -> SplitPlan:
    """Split `shift` at minute `at` and say who could work each part (SPEC §7.3)."""
    if not (shift.interval.start < at < shift.interval.end):
        raise ValueError("the split time must fall inside the shift")
    first = replace(shift, interval=Interval(shift.interval.start, at))
    second = Shift(
        None, None, shift.day, Interval(at, shift.interval.end), shift.pay_role_id, shift.location_id
    )
    trial = replace(ctx, shifts=[first if s is shift else s for s in ctx.shifts])
    return SplitPlan(first, second, who_could_take(trial, first), who_could_take(trial, second))


def split_result(
    ctx: ScheduleContext, shift: Shift, at: int, first_id: int | None, second_id: int | None
) -> tuple[list[Shift], ChangeCheck]:
    """The schedule after the split with each part assigned, and what the checks say."""
    first = replace(shift, interval=Interval(shift.interval.start, at), employee_id=first_id)
    second = Shift(
        None, second_id, shift.day, Interval(at, shift.interval.end), shift.pay_role_id, shift.location_id
    )
    after = [first if s is shift else s for s in ctx.shifts] + [second]
    return after, check_change(ctx, after)
