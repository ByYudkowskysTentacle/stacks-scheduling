# SPDX-License-Identifier: AGPL-3.0-or-later
"""The published schedule as employees see it, and exports of a published version.

Employees get a restricted projection (D6): day, times, location, pay-role colour and
co-workers' preferred names. Never availability, hours, wages, tags or notes about others.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.people.models import Employee, PayRole
from app.scheduling.models import PublishedVersion, SchedulePeriod
from domain import calendar as cal
from domain.timeutil import Interval, format_minute


@dataclass(frozen=True)
class PublicShift:
    day: date
    interval: Interval
    name: str  # preferred name, or "Open shift"
    employee_id: int | None
    color: str
    location: str
    note: str = ""  # shown only to the person the shift belongs to


@dataclass
class PublicDay:
    day: date
    shifts: list[PublicShift] = field(default_factory=lambda: [])


def _latest_versions(org, first: date, last: date) -> list[tuple[SchedulePeriod, PublishedVersion]]:
    out = []
    for period in SchedulePeriod.objects.filter(organization=org).select_related("location"):
        if period.end_date < first or period.start_date > last:
            continue
        latest = period.latest_version()
        if latest is not None:
            out.append((period, latest))
    return out


def published_week(org, week_start: date, viewer: Employee | None) -> list[PublicDay]:
    days = cal.week_days(week_start)
    names = dict(Employee.objects.filter(organization=org).values_list("pk", "preferred_name"))
    colors = dict(PayRole.objects.filter(organization=org).values_list("pk", "color"))
    by_day: dict[date, list[PublicShift]] = defaultdict(list)
    for period, version in _latest_versions(org, days[0], days[-1]):
        for row in version.shifts:
            day = date.fromisoformat(row["date"])
            if not days[0] <= day <= days[-1]:
                continue
            own = viewer is not None and row["employee_id"] == viewer.pk
            by_day[day].append(
                PublicShift(
                    day=day,
                    interval=Interval(row["start"], row["end"]),
                    name=names.get(row["employee_id"], "Open shift") if row["employee_id"] else "Open shift",
                    employee_id=row["employee_id"],
                    color=colors.get(row.get("pay_role_id"), "#5b6472"),
                    location=period.location.name,
                    note=row.get("note", "") if own else "",
                )
            )
    return [PublicDay(d, sorted(by_day.get(d, []), key=lambda s: (s.interval, s.name))) for d in days]


@dataclass(frozen=True)
class MyShift:
    shift: PublicShift
    with_me: list[str]


def my_upcoming(employee: Employee, from_day: date, weeks: int = 4) -> list[MyShift]:
    """The employee's published shifts with who else is on duty at the same time."""
    out: list[MyShift] = []
    start = cal.week_start_for(from_day, employee.organization.week_start)
    for w in range(weeks):
        for d in published_week(employee.organization, start + timedelta(days=7 * w), employee):
            if d.day < from_day:
                continue
            for s in d.shifts:
                if s.employee_id != employee.pk:
                    continue
                others = sorted(
                    {
                        o.name
                        for o in d.shifts
                        if o.employee_id not in (None, employee.pk) and o.interval.overlaps(s.interval)
                    }
                )
                out.append(MyShift(s, others))
    return out


EXPORT_COLUMNS = [
    "date",
    "weekday",
    "start",
    "end",
    "hours",
    "full_name",
    "preferred_name",
    "email",
    "pay_role",
    "note",
]


def export_csv(version: PublishedVersion) -> str:
    """A published version as CSV, one row per shift, for the Homebase fallback (D47) or any
    other tool. Supervisor-only: it holds names and emails."""
    org = version.organization
    employees = {e.pk: e for e in Employee.objects.filter(organization=org)}
    roles = dict(PayRole.objects.filter(organization=org).values_list("pk", "name"))
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(EXPORT_COLUMNS)
    rows = sorted(version.shifts, key=lambda r: (r["date"], r["start"], r["employee_id"] or 0))
    for r in rows:
        e = employees.get(r["employee_id"]) if r["employee_id"] else None
        day = date.fromisoformat(r["date"])
        writer.writerow(
            [
                day.isoformat(),
                cal.WEEKDAY_NAMES[day.weekday()],
                format_minute(r["start"]),
                format_minute(r["end"]),
                f"{(r['end'] - r['start']) / 60:g}",
                e.full_name if e else "",
                e.preferred_name if e else "Open shift",
                e.email if e else "",
                roles.get(r.get("pay_role_id"), ""),
                r.get("note", ""),
            ]
        )
    return buf.getvalue()


@dataclass
class GridRow:
    name: str
    cells: list[list[Interval]]
    minutes: int


def version_grid(version: PublishedVersion) -> tuple[list[date], list[GridRow]]:
    """People by days for printing a published version."""
    period = version.period
    days = period.days
    names = dict(
        Employee.objects.filter(organization=period.organization).values_list("pk", "preferred_name")
    )
    cells: dict[int | None, dict[date, list[Interval]]] = defaultdict(lambda: defaultdict(list))
    for r in version.shifts:
        cells[r["employee_id"]][date.fromisoformat(r["date"])].append(Interval(r["start"], r["end"]))
    rows = []
    for emp, by_day in cells.items():
        name = names.get(emp, "Open shifts") if emp else "Open shifts"
        rows.append(
            GridRow(
                name,
                [sorted(by_day.get(d, [])) for d in days],
                sum(iv.minutes for ivs in by_day.values() for iv in ivs),
            )
        )
    rows.sort(key=lambda r: (r.name == "Open shifts", r.name.lower()))
    return days, rows
