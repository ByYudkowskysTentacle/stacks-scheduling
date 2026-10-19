# SPDX-License-Identifier: AGPL-3.0-or-later
# pyright: basic
"""The CP-SAT schedule model (SPEC §7.2, ADR 0001).

One boolean per candidate shift. Hard rules are constraints; everything else is a weighted
term in one minimised objective. The model stays linear (no CP-only constraints), so CBC
remains a drop-in fallback (ADR 0001). Units inside the model are 15-minute slots.

Objective tiers (CLAUDE.md §4, SPEC §7.2), per slot unless noted. Each tier is sized so a
plausible amount of the tier below can't buy one unit of the tier above:

    W_HARD_TAG     2,000,000  a hard tag rule broken (kept soft so a solve is never infeasible,
                              but above coverage, so the generator leaves a gap before it
                              breaks a hard rule; D67)
    W_SHORTFALL    1,000,000  an uncovered person-slot: the most expensive thing after hard rules
    W_DUTY           200,000  an opener duty day missed (per day)
    W_TAG            100,000  a soft tag rule missed
    W_NOT_ENTERED     50,000  using someone whose availability isn't entered (SPEC §5.1)
    W_OVER            10,000  an over-coverage person-slot
    W_TARGET           1,000  short of a supervisor hour target, times its priority
    W_DUTY_HOURS      10,000  short of a lead-week duty's hours (a target with priority 10)
    W_DESIRED_FWS        500  short of approved desired hours, work-study (D34)
    W_DESIRED            300  short of approved desired hours, others
    W_PROTECT            800  a slot cut from someone below the median weekly hours, in
                              rebalances: part of the fairness tier, so above keeping
                              existing shifts; hours come from the busiest first (SPEC §6.4)
    W_KEEP               400  an existing slot moved, in rebalances (kept above the peak-load
                              term so small edits give small diffs: benchmark finding, D68)
    W_ROTATION           300  an off-week slot under `preferred` rotation
    W_FAIR               100  the highest weekly load among general staff
    W_SHIFT               20  per shift: fewer, longer shifts
    W_PREF                 1  bonus per slot worked inside a preferred block
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ortools.sat.python import cp_model

from domain.engine.candidates import Candidate
from domain.rules import CapScope, CoverageMode, RotationMode, TagRuleKind
from domain.schedule import ScheduleContext, Shift, slots_of
from domain.timeutil import SLOT_MINUTES, Interval, intersect

W_HARD_TAG = 2_000_000
W_SHORTFALL = 1_000_000
W_DUTY = 200_000
W_TAG = 100_000
W_NOT_ENTERED = 50_000
W_OVER = 10_000
W_DUTY_HOURS = 10_000
W_TARGET = 1_000
W_DESIRED_FWS = 500
W_DESIRED = 300
W_KEEP = 400
W_ROTATION = 300
W_FAIR = 100
W_PROTECT = 800
W_SHIFT = 20
W_PREF = 1

TIERS = (
    "hard_tag",
    "shortfall",
    "duty",
    "tag",
    "not_entered",
    "over",
    "duty_hours",
    "target",
    "desired",
    "keep",
    "rotation",
    "fair",
    "protect",
    "shift",
    "pref",
)


def slots(minutes: int) -> int:
    return -(-minutes // SLOT_MINUTES)


@dataclass
class Built:
    model: cp_model.CpModel
    cands: list[Candidate]
    x: list[Any]
    terms: dict[str, list[tuple[int, Any]]]  # objective terms by tier
    shortfall_vars: list[Any]
    existing_free: list[Shift]  # free existing shifts, for hours-moved accounting
    kept_exprs: list[tuple[int, Any]]  # (existing minutes, kept slots expression) per free existing shift
    hint_assignment: dict[int, int] = field(
        default_factory=lambda: {}
    )  # candidate index -> 0/1 (existing schedule)
    n_constraints: int = 0


def _overlap_slots(c: Candidate, iv: Interval) -> int:
    lo, hi = max(c.start, iv.start), min(c.end, iv.end)
    return max(0, slots(hi) - slots(lo)) if hi > lo else 0


def build_model(
    ctx: ScheduleContext,
    cands: list[Candidate],
    *,
    existing: list[Shift] | None = None,
    keep: bool = False,
    max_moved_minutes: int | None = None,
    protect: dict[tuple[int, int], int] | None = None,
) -> Built:
    rules = ctx.rules
    shape = rules.shape
    period = ctx.period
    m = cp_model.CpModel()
    x = [m.new_bool_var(f"x{i}") for i in range(len(cands))]
    terms: dict[str, list[tuple[int, Any]]] = defaultdict(list)
    n_cons = 0

    def add(constraint) -> None:
        nonlocal n_cons
        m.add(constraint)
        n_cons += 1

    for i, c in enumerate(cands):
        if c.fixed:
            add(x[i] == 1)

    by_person_day: dict[tuple[int, date], list[int]] = defaultdict(list)
    by_person_week: dict[tuple[int, int], list[int]] = defaultdict(list)
    by_cover: dict[tuple[date, int], list[int]] = defaultdict(list)
    for i, c in enumerate(cands):
        by_person_day[(c.employee_id, c.day)].append(i)
        by_person_week[(c.employee_id, period.week_of(c.day))].append(i)
        for slot in slots_of(c.interval):
            by_cover[(c.day, slot)].append(i)

    def length(i: int) -> int:
        return slots(cands[i].minutes)

    def fixed_sum(idxs: list[int]) -> int:
        return sum(length(i) for i in idxs if cands[i].fixed)

    def capped(idxs: list[int], limit_slots: int) -> None:
        """sum(len * x) <= limit, never tighter than what fixed shifts already use."""
        if not idxs or sum(length(i) for i in idxs) <= limit_slots:
            return
        add(sum(length(i) * x[i] for i in idxs) <= max(limit_slots, fixed_sum(idxs)))

    # Coverage (SPEC §6.2): on duty + under - over == target on every open slot.
    shortfall_vars = []
    slack: dict[tuple[date, int], tuple[Any, Any, int]] = {}
    for day in period.days:
        for iv in ctx.open_on(day):
            for slot in slots_of(iv):
                target = rules.coverage.target(day.weekday(), slot * SLOT_MINUTES)
                cover = by_cover.get((day, slot), [])
                u = m.new_int_var(0, target, f"u_{day}_{slot}")
                o = m.new_int_var(0, max(len(cover), 1), f"o_{day}_{slot}")
                add(sum(x[i] for i in cover) + u - o == target)
                shortfall_vars.append(u)
                slack[(day, slot)] = (u, o, target)
                terms["shortfall"].append((W_SHORTFALL, u))
                if rules.coverage.mode == CoverageMode.EXACT:
                    terms["over"].append((W_OVER, o))

    # Shift shape per person and day (SPEC §6.1).
    for idxs in by_person_day.values():
        free = [i for i in idxs if not cands[i].fixed]
        if not free:
            continue
        n_fixed = len(idxs) - len(free)
        if len(idxs) > shape.max_shifts_per_day:
            add(sum(x[i] for i in idxs) <= max(shape.max_shifts_per_day, n_fixed))
        capped(idxs, max(slots(shape.max_daily), 0))
        # No overlap and a minimum gap: at most one shift, extended by the gap, covers any minute.
        groups: dict[int, list[int]] = defaultdict(list)
        for i in idxs:
            c = cands[i]
            for t in range(c.start // SLOT_MINUTES, slots(c.end + shape.min_gap)):
                groups[t].append(i)
        seen: set[tuple[int, ...]] = set()
        for group in groups.values():
            key = tuple(group)
            if len(group) > 1 and key not in seen and sum(1 for i in group if cands[i].fixed) <= 1:
                seen.add(key)
                m.add_at_most_one([x[i] for i in group])
                n_cons += 1

    # Caps (SPEC §6.4): hard.
    people = ctx.people
    for pid, person in people.items():
        for cap in rules.caps:
            applies_to = cap.employee_id if cap.employee_id is not None else None
            if applies_to is not None and applies_to != pid:
                continue
            if cap.scope == CapScope.WEEK:
                for week in range(period.weeks):
                    if cap.tag is not None and not person.tagged_in(cap.tag, period.week_days(week)):
                        continue
                    capped(by_person_week.get((pid, week), []), cap.minutes // SLOT_MINUTES)
            else:
                if cap.tag is not None and not person.tagged_in(cap.tag, period.days):
                    continue
                idxs = [i for w in range(period.weeks) for i in by_person_week.get((pid, w), [])]
                outside = ctx.term_minutes_outside_period.get(pid, 0) if cap.scope == CapScope.TERM else 0
                capped(idxs, max(0, (cap.minutes - outside) // SLOT_MINUTES))

    # Week-hours duties (scenario 4): the lead week's hours as a high-priority target, and a
    # hard ceiling on the other weeks.
    for duty in rules.week_duties:
        for week in range(period.weeks):
            idxs = by_person_week.get((duty.employee_id, week), [])
            if week in duty.weeks:
                need = slots(duty.minutes)
                d = m.new_int_var(0, need, f"duty_{duty.employee_id}_{week}")
                add(sum(length(i) * x[i] for i in idxs) + d >= need)
                capped(idxs, need)
                terms["duty_hours"].append((W_DUTY_HOURS, d))
            else:
                capped(idxs, duty.off_week_max // SLOT_MINUTES)

    # Opener duty (scenario 3): a shift starting at opening time on each listed weekday.
    for o in rules.openers:
        for day in period.days:
            if day.weekday() not in o.weekdays:
                continue
            open_ivs = ctx.open_on(day)
            if not open_ivs:
                continue
            opening = open_ivs[0].start
            idxs = [i for i in by_person_day.get((o.employee_id, day), []) if cands[i].start == opening]
            miss = m.new_bool_var(f"open_miss_{o.employee_id}_{day}")
            add(sum(x[i] for i in idxs) + miss >= 1)
            terms["duty"].append((W_DUTY, miss))

    # Tag coverage rules (SPEC §6.3).
    def tagged(i: int, tag: str) -> bool:
        p = people.get(cands[i].employee_id)
        return p is not None and p.has_tag(tag, cands[i].day)

    for r_i, rule in enumerate(rules.tag_rules):
        weight = W_HARD_TAG if rule.hard else W_TAG
        if rule.kind == TagRuleKind.MIN_ON_DUTY:
            for day in period.days:
                if day.weekday() not in rule.weekdays:
                    continue
                window = ctx.open_on(day)
                if rule.interval is not None:
                    window = intersect(window, [rule.interval])
                for iv in window:
                    for slot in slots_of(iv):
                        have = [i for i in by_cover.get((day, slot), []) if tagged(i, rule.tag)]
                        miss = m.new_int_var(0, rule.count, f"tag{r_i}_{day}_{slot}")
                        add(sum(x[i] for i in have) + miss >= rule.count)
                        terms["hard_tag" if rule.hard else "tag"].append((weight, miss))
        elif rule.kind == TagRuleKind.MIN_WEEKLY_HOURS:
            for week in range(period.weeks):
                days = set(period.week_days(week))
                idxs = [i for i, c in enumerate(cands) if c.day in days and tagged(i, rule.tag)]
                need = slots(rule.minutes)
                d = m.new_int_var(0, need, f"tagh{r_i}_{week}")
                add(sum(length(i) * x[i] for i in idxs) + d >= need)
                terms["hard_tag" if rule.hard else "tag"].append((max(1, weight // 4), d))
        elif rule.kind == TagRuleKind.NOT_ALONE:
            # tagged <= K * (untagged + miss), with untagged read from the coverage equation
            # (on duty = target - under + over) so only tagged candidates are listed.
            k = 4
            for (day, slot), (u, o, target) in slack.items():
                new = [i for i in by_cover.get((day, slot), []) if tagged(i, rule.tag)]
                if not new:
                    continue
                miss = m.new_bool_var(f"alone{r_i}_{day}_{slot}")
                add(sum((1 + k) * x[i] for i in new) + k * u - k * o - k * miss <= k * target)
                terms["hard_tag" if rule.hard else "tag"].append((weight, miss))
        elif rule.kind == TagRuleKind.NOT_SOLO:
            # Whenever a tagged candidate is on, at least two people are on duty in that slot
            # (on duty = target - under + over, as above).
            for (day, slot), (u, o, target) in slack.items():
                new = [i for i in by_cover.get((day, slot), []) if tagged(i, rule.tag)]
                if not new:
                    continue
                miss = m.new_bool_var(f"solo{r_i}_{day}_{slot}")
                for i in new:
                    add(target - u + o + 2 * miss >= 2 * x[i])
                terms["hard_tag" if rule.hard else "tag"].append((weight, miss))

    # Supervisor hour targets (SPEC §6.4), soft and below caps.
    for t in rules.targets:
        for week in range(period.weeks):
            if t.weeks is not None and week not in t.weeks:
                continue
            need = slots(t.minutes)
            d = m.new_int_var(0, need, f"tgt_{t.employee_id}_{week}")
            add(sum(length(i) * x[i] for i in by_person_week.get((t.employee_id, week), [])) + d >= need)
            terms["target"].append((W_TARGET * max(1, t.priority), d))

    # Approved desired hours (D32, D34). Two-piece deficit: the deeper half costs double, so
    # hours go first to whoever is furthest below.
    for want in rules.desired:
        w = W_DESIRED_FWS if want.work_study else W_DESIRED
        for week in range(period.weeks):
            need = slots(want.minutes)
            half = need // 2
            d1 = m.new_int_var(0, need - half, f"des1_{want.employee_id}_{week}")
            d2 = m.new_int_var(0, half, f"des2_{want.employee_id}_{week}")
            add(
                sum(length(i) * x[i] for i in by_person_week.get((want.employee_id, week), [])) + d1 + d2
                >= need
            )
            terms["desired"].append((w, d1))
            terms["desired"].append((2 * w, d2))

    # Fairness: minimise the highest weekly load among general staff (no duty, target, desired
    # hours or new-employee cap, and availability entered).
    special = rules.duty_employee_ids() | {t.employee_id for t in rules.targets}
    special |= {d.employee_id for d in rules.desired}
    special |= {c.employee_id for c in rules.caps if c.employee_id is not None}
    peak = m.new_int_var(0, 7 * 24 * 4, "peak")
    for pid, person in people.items():
        if pid in special or not person.auto_schedulable or not person.availability.is_known:
            continue
        for week in range(period.weeks):
            idxs = by_person_week.get((pid, week), [])
            if idxs:
                add(sum(length(i) * x[i] for i in idxs) <= peak)
    terms["fair"].append((W_FAIR, peak))

    # Protected minimum in rebalances: don't push low-hour people lower.
    for (pid, week), before in (protect or {}).items():
        cut = m.new_int_var(0, before, f"cut_{pid}_{week}")
        add(sum(length(i) * x[i] for i in by_person_week.get((pid, week), [])) + cut >= before)
        terms["protect"].append((W_PROTECT, cut))

    # Per-candidate terms.
    for i, c in enumerate(cands):
        person = people.get(c.employee_id)
        if person is None:
            continue
        n = length(i)
        terms["shift"].append((W_SHIFT, x[i]))
        pref = sum(
            max(0, slots(min(c.end, iv.end)) - slots(max(c.start, iv.start)))
            for iv in person.availability.preferred_for_scheduling(c.day.weekday())
            if iv.overlaps(c.interval)
        )
        if pref:
            terms["pref"].append((-W_PREF * pref, x[i]))
        if not person.availability.is_known and not c.fixed:
            terms["not_entered"].append((W_NOT_ENTERED * n, x[i]))
        if rules.rotation.mode == RotationMode.PREFERRED and not rules.rotation.on_week(
            c.employee_id, period.week_of(c.day)
        ):
            terms["rotation"].append((W_ROTATION * n, x[i]))

    # Keeping existing shifts (rebalance, D33, D38): a slot of an existing shift is kept when the
    # same person still works it that day, so a retime only counts its non-overlapping part.
    existing_free = [s for s in (existing or []) if s.employee_id is not None]  # assigned shifts only
    fixed_ids = {c.existing_id for c in cands if c.fixed and c.existing_id is not None}
    existing_free = [s for s in existing_free if s.id not in fixed_ids]
    kept_exprs: list[tuple[int, Any]] = []
    if keep or max_moved_minutes is not None:
        for s in existing_free:
            idxs = [
                i
                for i in by_person_day.get((s.employee_id or 0, s.day), [])
                if cands[i].interval.overlaps(s.interval)
            ]
            kept = sum(_overlap_slots(cands[i], s.interval) * x[i] for i in idxs) if idxs else 0
            kept_exprs.append((slots(s.minutes), kept))
            if keep and idxs:
                terms["keep"].append((-W_KEEP, kept))  # minimising moved = maximising kept
        if max_moved_minutes is not None and kept_exprs:
            total = sum(n for n, _ in kept_exprs)
            add(sum(k for _, k in kept_exprs if not isinstance(k, int)) >= total - slots(max_moved_minutes))

    m.minimize(cp_model.LinearExpr.sum([coef * var for tier in terms.values() for coef, var in tier]))

    hint: dict[int, int] = {}
    if existing is not None:
        index = {c.key: i for i, c in enumerate(cands)}
        keys = [
            (s.employee_id, s.day, s.interval.start, s.interval.end)
            for s in existing
            if s.employee_id is not None
        ]
        chosen = {index[k] for k in keys if k in index}
        hint = {i: (1 if i in chosen else 0) for i in range(len(cands))}

    return Built(m, cands, x, dict(terms), shortfall_vars, existing_free, kept_exprs, hint, n_cons)
