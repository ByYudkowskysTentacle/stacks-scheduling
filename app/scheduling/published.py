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

from app.core.models import Department
from app.people.models import Employee, PayRole
from app.scheduling.models import PublishedVersion, SchedulePeriod
from domain import calendar as cal
from domain.timeutil import Interval, format_minute

NEUTRAL_CHIP = "#5b6472"


@dataclass(frozen=True)
class PublicShift:
    day: date
    interval: Interval
    name: str  # preferred name, or "Open shift"
    employee_id: int | None
    color: str
    location: str
    note: str = ""  # shown only to the person the shift belongs to
    shift_id: int | None = None
    role: str = ""  # pay role name: only on your own shifts and open shifts (D112)


@dataclass
class PublicDay:
    day: date
    shifts: list[PublicShift] = field(default_factory=lambda: [])


def student_departments(employee: Employee) -> frozenset[int] | None:
    """The departments whose published schedules `employee` sees, and where they can swap or
    cover (D108). None when the organization has no departments: then everything is shared."""
    if not Department.objects.filter(organization_id=employee.organization_id).exists():
        return None
    return frozenset(employee.departments.values_list("pk", flat=True))


def open_shift_departments(employee: Employee) -> frozenset[int] | None:
    """Their own departments, plus any whose open shifts one of them has chosen to show (D108)."""
    own = student_departments(employee)
    if own is None:
        return None
    extra = {
        int(pk)
        for chosen in Department.objects.filter(pk__in=own).values_list("open_shifts_from", flat=True)
        for pk in chosen or []
    }
    return own | frozenset(extra)


def _latest_versions(
    org, first: date, last: date, department_ids: frozenset[int] | None = None
) -> list[tuple[SchedulePeriod, PublishedVersion]]:
    out = []
    periods = SchedulePeriod.objects.filter(organization=org).select_related("location")
    if department_ids is not None:
        periods = periods.filter(department_id__in=department_ids)
    for period in periods:
        if period.end_date < first or period.start_date > last:
            continue
        latest = period.latest_version()
        if latest is not None:
            out.append((period, latest))
    return out


def published_week(
    org, week_start: date, viewer: Employee | None, department_ids: frozenset[int] | None = None
) -> list[PublicDay]:
    """One week of the published schedule; `department_ids` limits it to those departments."""
    days = cal.week_days(week_start)
    names = dict(Employee.objects.filter(organization=org).values_list("pk", "preferred_name"))
    roles = {r.pk: r for r in PayRole.objects.filter(organization=org)}
    by_day: dict[date, list[PublicShift]] = defaultdict(list)
    for period, version in _latest_versions(org, days[0], days[-1], department_ids):
        for row in version.shifts:
            day = date.fromisoformat(row["date"])
            if not days[0] <= day <= days[-1]:
                continue
            own = viewer is not None and row["employee_id"] == viewer.pk
            # A co-worker's pay role can reveal work-study status, so it shows (as colour and
            # name) only on your own shifts and open shifts; others get a neutral bar (D112).
            role = roles.get(row.get("pay_role_id")) if own or not row["employee_id"] else None
            by_day[day].append(
                PublicShift(
                    day=day,
                    interval=Interval(row["start"], row["end"]),
                    name=names.get(row["employee_id"], "Open shift") if row["employee_id"] else "Open shift",
                    employee_id=row["employee_id"],
                    color=role.color if role else NEUTRAL_CHIP,
                    location=period.location.name,
                    note=row.get("note", "") if own else "",
                    shift_id=row.get("id"),
                    role=role.name if role else "",
                )
            )
    return [PublicDay(d, sorted(by_day.get(d, []), key=lambda s: (s.interval, s.name))) for d in days]


@dataclass(frozen=True)
class MyShift:
    shift: PublicShift
    with_me: list[str]


def published_horizon(org, department_ids: frozenset[int] | None = None) -> date | None:
    """The last day any published schedule covers, or None if nothing is published."""
    periods = SchedulePeriod.objects.filter(organization=org, versions__isnull=False).distinct()
    if department_ids is not None:
        periods = periods.filter(department_id__in=department_ids)
    return max((p.end_date for p in periods), default=None)


def next_published_day(
    org, after: date, viewer: Employee | None = None, department_ids: frozenset[int] | None = None
) -> date | None:
    """The first day on or after `after` with a published shift (the viewer's own, if given)."""
    days = [
        date.fromisoformat(row["date"])
        for _period, version in _latest_versions(org, after, date.max, department_ids)
        for row in version.shifts
        if viewer is None or row["employee_id"] == viewer.pk
    ]
    return min((d for d in days if d >= after), default=None)


def my_upcoming(employee: Employee, from_day: date, weeks: int | None = None) -> list[MyShift]:
    """The employee's published shifts with who else is on duty at the same time. With no
    `weeks`, it looks as far ahead as anything is published, so no published shift is hidden
    by a fixed window."""
    out: list[MyShift] = []
    start = cal.week_start_for(from_day, employee.organization.week_start)
    if weeks is None:
        horizon = published_horizon(employee.organization)
        weeks = 0 if horizon is None or horizon < from_day else (horizon - start).days // 7 + 1
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


@dataclass
class WeekRow:
    name: str
    cells: list[list[Interval]]
    week_minutes: int
    period_minutes: int


def version_weeks(version: PublishedVersion) -> list[dict]:
    """The version grid one week at a time, leaving out people with no shifts that week (audit 2.13)."""
    days, rows = version_grid(version)
    weeks = []
    for start in range(0, len(days), 7):
        week_rows = []
        for r in rows:
            cells = r.cells[start : start + 7]
            if any(cells):
                week_rows.append(
                    WeekRow(r.name, cells, sum(iv.minutes for ivs in cells for iv in ivs), r.minutes)
                )
        weeks.append({"days": days[start : start + 7], "rows": week_rows})
    return weeks
