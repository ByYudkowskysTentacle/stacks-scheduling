"""Independent checker for benchmark results (a subset of the SPEC §8 validation suite).

It recomputes everything from the returned shifts and the roster, without looking at
solver variables, so a modelling bug shows up as a violation instead of passing quietly.
"""

from __future__ import annotations

from collections import defaultdict

from model import Candidate
from synth import (
    FRI,
    MON,
    OPERATING_INTERVALS,
    PERIOD_DAYS,
    SLOTS_PER_HOUR,
    Roster,
    hours,
    open_slots,
    opening_slot,
    slot,
    week_of,
    weekday_of,
)


def check(roster: Roster, shifts: list[Candidate]) -> dict:
    r, d = roster.rules, roster.duties
    emps = {e.id: e for e in roster.employees}
    hard: list[str] = []
    per_day: dict[tuple, list[Candidate]] = defaultdict(list)
    week_slots: dict[tuple, int] = defaultdict(int)
    cover: dict[tuple, list[str]] = defaultdict(list)

    for s in shifts:
        e = emps[s.emp]
        wd = weekday_of(s.day)
        per_day[(s.emp, s.day)].append(s)
        week_slots[(s.emp, week_of(s.day))] += s.length
        if e.employment_type != "regular":
            hard.append(f"{s.emp} is {e.employment_type} but was auto-scheduled")
        if not any(a <= s.start and s.end <= b for a, b in OPERATING_INTERVALS[wd]):
            hard.append(f"{s.emp} day {s.day}: shift outside open hours or across a closure")
        if any(s.start < b and a < s.end for a, b in e.unavailable.get(wd, [])):
            hard.append(f"{s.emp} day {s.day}: shift overlaps unavailable time")
        is_opener_exception = (
            s.emp == d.opener and wd in d.opener_short_weekdays and s.start == opening_slot(wd)
            and s.length == d.opener_short_slots
        )
        if not (r.min_len <= s.length <= r.max_len) and not is_opener_exception:
            hard.append(f"{s.emp} day {s.day}: length {hours(s.length)}h outside shape rules")
        if r.rotation == "strict" and e.rotation_group and (e.rotation_group == "A") != (week_of(s.day) == 0):
            hard.append(f"{s.emp} day {s.day}: works outside their strict rotation week")
        for t in range(s.start, s.end):
            cover[(s.day, t)].append(s.emp)

    for (emp, day), day_shifts in per_day.items():
        day_shifts.sort(key=lambda s: s.start)
        if len(day_shifts) > r.max_shifts_per_day:
            hard.append(f"{emp} day {day}: {len(day_shifts)} shifts")
        if sum(s.length for s in day_shifts) > r.max_day_slots:
            hard.append(f"{emp} day {day}: over daily hours")
        for a, b in zip(day_shifts, day_shifts[1:]):
            if b.start - a.end < r.min_gap:
                hard.append(f"{emp} day {day}: gap under {r.min_gap * 15} minutes")

    lead_weeks = dict(d.leads)
    for e in roster.employees:
        for week in (0, 1):
            h = week_slots[(e.id, week)]
            if e.id in lead_weeks:
                want = d.lead_week_hours if week == lead_weeks[e.id] else None
                if want is not None and h != want * SLOTS_PER_HOUR:
                    hard.append(f"{e.id} lead week {week}: {hours(h)}h, expected {want}h")
                if want is None and h > d.lead_off_week_max * SLOTS_PER_HOUR:
                    hard.append(f"{e.id} off week {week}: {hours(h)}h over {d.lead_off_week_max}h")
            elif h > e.weekly_cap * SLOTS_PER_HOUR:
                hard.append(f"{e.id} week {week}: {hours(h)}h over weekly cap {e.weekly_cap}h")
        if e.period_cap is not None:
            total = week_slots[(e.id, 0)] + week_slots[(e.id, 1)]
            if total > e.period_cap * SLOTS_PER_HOUR:
                hard.append(f"{e.id}: {hours(total)}h over period cap {e.period_cap}h")

    if d.opener:
        for day in range(PERIOD_DAYS):
            wd = weekday_of(day)
            if MON <= wd <= FRI and not any(s.start == opening_slot(wd) for s in per_day[(d.opener, day)]):
                hard.append(f"opener {d.opener} does not open on day {day}")

    under = over = lead_miss = cat_miss = new_alone = 0
    for day in range(PERIOD_DAYS):
        wd = weekday_of(day)
        for t in open_slots(wd):
            on = cover[(day, t)]
            under += max(0, r.coverage_target - len(on))
            over += max(0, len(on) - r.coverage_target)
            tags = [emps[p].tags for p in on]
            lead_miss += not any("Shift lead" in tg for tg in tags)
            if MON <= wd <= FRI and slot(9) <= t < slot(17):
                cat_miss += not any("Cataloguer" in tg for tg in tags)
            if on and all("New employee" in tg for tg in tags):
                new_alone += 1
    processor_short = [
        max(0.0, 15 - hours(sum(week_slots[(e.id, w)] for e in roster.employees if "Processor" in e.tags)))
        for w in (0, 1)
    ]
    not_entered_hours = sum(
        hours(s.length) for s in shifts if emps[s.emp].availability_status == "not_entered"
    )
    weekly = {e.id: [hours(week_slots[(e.id, w)]) for w in (0, 1)] for e in roster.employees}
    target_short = {
        e.id: [max(0.0, e.target_week_hours - h) for h in weekly[e.id]]
        for e in roster.employees
        if e.target_week_hours is not None
    }
    return {
        "hard_violations": hard,
        "shortfall_hours": hours(under),
        "over_hours": hours(over),
        "lead_miss_hours": hours(lead_miss),
        "cataloguer_miss_hours": hours(cat_miss),
        "new_alone_hours": hours(new_alone),
        "processor_short_hours": processor_short,
        "not_entered_hours": not_entered_hours,
        "target_short_hours": target_short,
        "max_week_hours": max(max(v) for v in weekly.values()),
        "weekly_hours": weekly,
        "n_shifts": len(shifts),
    }
