# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reports (SPEC §14): scheduled hours against limits, and coverage. Each exports to CSV and
JSON and prints cleanly. Built from the working copy, so drafts are included and labelled."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import asdict, dataclass
from datetime import date

from app.scheduling import services
from app.scheduling.models import DesiredHours, DesiredStatus, HourTargetRule, SchedulePeriod
from domain.schedule import on_duty_by_slot, slots_of
from domain.timeutil import SLOT_MINUTES, total_minutes
from domain.validation import Category


def _h(minutes: int | None) -> float | None:
    return None if minutes is None else round(minutes / 60, 2)


@dataclass
class HoursLine:
    name: str
    weeks: list[float]
    weekly_caps: list[float | None]
    period_hours: float
    period_cap: float | None
    target: float | None
    desired: float | None
    over_limit: bool
    below_target: bool


def hours_report(period: SchedulePeriod) -> list[HoursLine]:
    ctx = services.build_context(period)
    employees = list(services.employees_for_grid(period))
    rows = services.hours_rows(period, employees, ctx)
    targets = dict(
        HourTargetRule.objects.filter(organization=period.organization).values_list("employee_id", "minutes")
    )
    term = services.term_for(period)
    desired = (
        dict(
            DesiredHours.objects.filter(
                organization=period.organization, term=term, status=DesiredStatus.APPROVED
            ).values_list("employee_id", "minutes")
        )
        if term
        else {}
    )
    out = []
    for r in sorted(rows, key=lambda r: r.employee.display_name.lower()):
        target = targets.get(r.employee.pk)
        out.append(
            HoursLine(
                name=r.employee.display_name,
                weeks=[_h(m) or 0.0 for m, _ in r.weeks],
                weekly_caps=[_h(c) for _, c in r.weeks],
                period_hours=_h(r.period_minutes) or 0.0,
                period_cap=_h(r.period_cap),
                target=_h(target),
                desired=_h(desired.get(r.employee.pk)),
                over_limit=r.over,
                below_target=target is not None and any(m < target for m, _ in r.weeks),
            )
        )
    return out


@dataclass
class CoverageLine:
    day: date
    open_hours: float
    needed_person_hours: float
    scheduled_person_hours: float
    short_person_hours: float
    over_person_hours: float
    tag_rule_misses: int


def coverage_report(period: SchedulePeriod) -> list[CoverageLine]:
    ctx = services.build_context(period)
    report = services.validate(ctx)
    duty = on_duty_by_slot(ctx.shifts)
    out = []
    for day in ctx.period.days:
        open_ivs = ctx.open_on(day)
        needed = scheduled = 0
        for iv in open_ivs:
            for slot in slots_of(iv):
                needed += ctx.rules.coverage.target(day.weekday(), slot * SLOT_MINUTES)
                scheduled += len(duty[day][slot])
        issues = [i for i in report.issues if i.day == day]
        out.append(
            CoverageLine(
                day=day,
                open_hours=_h(total_minutes(open_ivs)) or 0.0,
                needed_person_hours=needed * SLOT_MINUTES / 60,
                scheduled_person_hours=scheduled * SLOT_MINUTES / 60,
                short_person_hours=sum(i.minutes for i in issues if i.category == Category.COVERAGE_GAP) / 60,
                over_person_hours=sum(i.minutes for i in issues if i.category == Category.OVER_COVERAGE) / 60,
                tag_rule_misses=sum(1 for i in issues if i.category == Category.TAG_RULE),
            )
        )
    return out


def to_csv(lines: list) -> str:
    buf = io.StringIO()
    if not lines:
        return ""
    rows = [_flat(asdict(line)) for line in lines]
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def to_json(lines: list, period: SchedulePeriod, kind: str) -> str:
    return json.dumps(
        {
            "report": kind,
            "period": {"start": period.start_date.isoformat(), "end": period.end_date.isoformat()},
            "source": "working copy (includes unpublished drafts)",
            "rows": [asdict(line) for line in lines],
        },
        default=str,
        indent=2,
    )


def _flat(row: dict) -> dict:
    """Spread per-week lists into week_1, week_2 ... columns for CSV."""
    out: dict = {}
    for k, v in row.items():
        if isinstance(v, list):
            for i, item in enumerate(v, start=1):
                out[f"{k}_{i}"] = "" if item is None else item
        else:
            out[k] = "" if v is None else v
    return out
