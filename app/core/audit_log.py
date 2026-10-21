# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read side of the audit log: events for one organization, described for people."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from django.apps import apps
from django.db.models import Q, QuerySet
from pghistory.models import Events

from app.accounts.models import User

HIDDEN_FIELDS = {"id", "organization_id", "created_at"}
ACTIONS = {"insert": "Added", "update": "Changed", "delete": "Removed"}


# Organization-wide settings: every supervisor sees their history (D107).
ORGANIZATION_WIDE = {
    "core.Organization",
    "core.Location",
    "core.OperatingHoursSet",
    "core.OpenInterval",
    "core.Closure",
    "core.Term",
    "core.Department",
    "core.Membership",
    "core.SupervisorDepartment",
    "core.RetentionPolicy",
    "people.PayRole",
    "people.Tag",
    "imports.ImportBatch",  # imports stay organization-wide for now (open item 17)
    "scheduling.SchedulingRules",
}
# Records that belong to a person, keyed by the employee they belong to.
BY_EMPLOYEE = {
    "people.EmployeePayRole",
    "people.EmployeeDepartment",
    "people.EmployeeTag",
    "availability.Availability",
    "availability.TimeOff",
    "scheduling.RotationAssignment",
    "scheduling.DutyAssignment",
    "scheduling.HourTargetRule",
    "scheduling.DesiredHours",
    "scheduling.CalendarFeed",
}
BY_PERIOD = {"scheduling.Shift", "scheduling.PublishedVersion", "scheduling.SolveRun"}
BY_DEPARTMENT = {"scheduling.CoverageOverride", "scheduling.TagCoverageRule"}  # blank: every department


def events_for(org, access=None) -> QuerySet:
    """The organization's audit entries, newest first. A supervisor limited to some departments
    sees the history of organization-wide settings and of records in their departments only
    (D107); admins see everything. A model not listed here is hidden from them."""
    rows = Events.objects.filter(
        Q(pgh_data__organization_id=org.pk) | Q(pgh_obj_model="core.Organization", pgh_obj_id=str(org.pk))
    )
    if access is not None and access.department_ids is not None:
        rows = rows.filter(_visible(org, access.department_ids))
    return rows.order_by("-pgh_created_at", "-pgh_id")


def _visible(org, department_ids) -> Q:
    from app.availability.models import Availability
    from app.people.models import Employee
    from app.scheduling.models import SchedulePeriod, Shift

    depts = list(department_ids)
    employees = list(
        Employee.objects.filter(organization=org, departments__in=depts)
        .values_list("pk", flat=True)
        .distinct()
    )
    users = list(
        Employee.objects.filter(pk__in=employees, user__isnull=False).values_list("user_id", flat=True)
    )
    periods = list(
        SchedulePeriod.objects.filter(organization=org, department__in=depts).values_list("pk", flat=True)
    )
    shifts = list(Shift.objects.filter(period__in=periods).values_list("pk", flat=True))
    availabilities = list(Availability.objects.filter(employee__in=employees).values_list("pk", flat=True))

    def model(*labels: str) -> Q:
        return Q(pgh_obj_model__in=labels)

    return (
        model(*ORGANIZATION_WIDE)
        | model("people.Employee") & Q(pgh_obj_id__in=[str(e) for e in employees])
        | model(*BY_EMPLOYEE) & Q(pgh_data__employee_id__in=employees)
        | model("scheduling.HourCapRule")
        & (Q(pgh_data__employee_id=None) | Q(pgh_data__employee_id__in=employees))
        | model("availability.AvailabilityBlock") & Q(pgh_data__availability_id__in=availabilities)
        | model("scheduling.SchedulePeriod") & Q(pgh_data__department_id__in=depts)
        | model(*BY_PERIOD) & Q(pgh_data__period_id__in=periods)
        | model("scheduling.CallOut") & Q(pgh_data__shift_id__in=shifts)
        | model("scheduling.ShiftExchange")
        & (Q(pgh_data__shift_id__in=shifts) | Q(pgh_data__other_shift_id__in=shifts))
        | model(*BY_DEPARTMENT) & (Q(pgh_data__department_id=None) | Q(pgh_data__department_id__in=depts))
        | model("scheduling.EmailPreference") & Q(pgh_data__user_id__in=users)
    )


@dataclass
class AuditRow:
    when: datetime
    who: str
    action: str
    what: str
    object_id: str
    changes: list[tuple[str, Any, Any]]
    summary: str


def _verbose(model_label: str) -> str:
    try:
        return str(apps.get_model(model_label)._meta.verbose_name).capitalize()
    except LookupError:
        return model_label


def _summary(data: dict) -> str:
    for key in ("name", "preferred_name", "reason", "role"):
        if data.get(key):
            return str(data[key])
    return ""


def filtered(rows: QuerySet, params) -> QuerySet:
    """Narrow the log by what changed, who changed it and when (audit 2.24)."""
    if what := params.get("what"):
        rows = rows.filter(pgh_obj_model=what)
    if (who := params.get("who", "")).isdigit():
        rows = rows.filter(pgh_context__user=int(who))
    elif who == "system":
        rows = rows.filter(Q(pgh_context__isnull=True) | Q(pgh_context__user__isnull=True))
    for key, lookup in (("from", "pgh_created_at__date__gte"), ("to", "pgh_created_at__date__lte")):
        try:
            rows = rows.filter(**{lookup: date.fromisoformat(params.get(key, ""))})
        except ValueError:
            pass
    return rows


def _people(events) -> dict[str, dict[int, str]]:
    """Names for the employee and availability ids in a page of events, so a row can say whose
    record changed instead of a database id."""
    from app.availability.models import Availability
    from app.people.models import Employee

    employee_ids = {d.get("employee_id") for e in events if (d := e.pgh_data or {}).get("employee_id")}
    availability_ids = {
        d.get("availability_id") for e in events if (d := e.pgh_data or {}).get("availability_id")
    }
    return {
        "employee": dict(Employee.objects.filter(pk__in=employee_ids).values_list("pk", "preferred_name")),
        "availability": dict(
            Availability.objects.filter(pk__in=availability_ids).values_list("pk", "employee__preferred_name")
        ),
    }


def describe(events) -> list[AuditRow]:
    events = list(events)
    user_ids = {e.pgh_context.get("user") for e in events if e.pgh_context and e.pgh_context.get("user")}
    emails = dict(User.objects.filter(pk__in=user_ids).values_list("pk", "email"))
    people = _people(events)
    rows = []
    for e in events:
        ctx = e.pgh_context or {}
        who = emails.get(ctx.get("user"), "System" if not ctx.get("user") else "Deleted user")
        changes = [
            (field, before, after)
            for field, (before, after) in sorted((e.pgh_diff or {}).items())
            if field not in HIDDEN_FIELDS
        ]
        rows.append(
            AuditRow(
                when=e.pgh_created_at,
                who=who,
                action=ACTIONS.get(e.pgh_label, e.pgh_label),
                what=_verbose(e.pgh_obj_model),
                object_id=str(e.pgh_obj_id),
                changes=changes,
                summary=_summary(e.pgh_data or {}) or _whose(e.pgh_data or {}, people),
            )
        )
    return rows


def _whose(data: dict, people: dict[str, dict[int, str]]) -> str:
    if name := people["employee"].get(data.get("employee_id")):
        return f"for {name}"
    if name := people["availability"].get(data.get("availability_id")):
        return f"for {name}"
    return ""
