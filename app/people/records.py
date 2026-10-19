# SPDX-License-Identifier: AGPL-3.0-or-later
"""Single-person data export and erasure (CLAUDE.md §6, SPEC §11): for records requests and
offboarding.

Erasure anonymises the employee rather than deleting the row, so shifts, published versions
and hour totals stay consistent (CLAUDE.md §2.3). It removes their availability, time off,
wages, tags, caps, duties, targets, desired hours and notes, and closes their sign-in account.
The audit log and stored messages are kept as the record of what happened (D100, D101); the
erasure itself is recorded there as an update made under the `erase_employee` context.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pghistory
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from app.people.models import Employee

PERSONAL_FIELDS = (
    "full_name",
    "preferred_name",
    "email",
    "campus_id",
    "notes",
    "default_wage",
    "hire_date",
    "new_employee_until",
)


class ErasureError(Exception):
    pass


def _plain(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def export_employee(employee: Employee) -> dict[str, Any]:
    """Everything Stacks holds about one person, as JSON-ready data."""
    from app.availability.models import Availability, TimeOff
    from app.scheduling.models import (
        CallOut,
        DesiredHours,
        DutyAssignment,
        EmailPreference,
        HourCapRule,
        HourTargetRule,
        Notification,
        PublishedVersion,
        RotationAssignment,
        Shift,
        ShiftExchange,
    )

    profile = {f: _plain(getattr(employee, f)) for f in PERSONAL_FIELDS}
    profile.update(
        employment_type=employee.employment_type,
        default_pay_role=str(employee.default_pay_role or ""),
        in_scheduling_pool=employee.in_scheduling_pool,
        shift_lead_access=employee.shift_lead_access,
        archived_at=_plain(employee.archived_at),
        created_at=_plain(employee.created_at),
    )
    data: dict[str, Any] = {
        "exported_at": timezone.now().isoformat(),
        "organization": employee.organization.name,
        "employee": profile,
        "pay_role_rates": [
            {"pay_role": r.pay_role.name, "wage": _plain(r.wage)}
            for r in employee.pay_roles.select_related("pay_role")
        ],
        "departments": [d.name for d in employee.departments.all()],
        "tags": [
            {"tag": t.tag.name, "from": _plain(t.valid_from), "to": _plain(t.valid_to)}
            for t in employee.employee_tags.select_related("tag")
        ],
        "availability": [
            {
                "term": a.term.name,
                "status": a.status,
                "blocks": [
                    {
                        "weekday": b.get_weekday_display(),
                        "kind": b.kind,
                        "start": b.start_min,
                        "end": b.end_min,
                    }
                    for b in a.blocks.all()
                ],
            }
            for a in Availability.objects.filter(employee=employee)
            .select_related("term")
            .prefetch_related("blocks")
        ],
        "desired_hours": [
            {"term": d.term.name, "hours": d.minutes / 60, "status": d.status}
            for d in DesiredHours.objects.filter(employee=employee).select_related("term")
        ],
        "time_off": [
            {"when": t.label(), "status": t.status, "requested_at": _plain(t.created_at)}
            for t in TimeOff.objects.filter(employee=employee)
        ],
        "hour_caps": [str(c) for c in HourCapRule.objects.filter(employee=employee)],
        "hour_targets": [str(t) for t in HourTargetRule.objects.filter(employee=employee)],
        "duties": [str(d) for d in DutyAssignment.objects.filter(employee=employee)],
        "rotation_group": getattr(
            RotationAssignment.objects.filter(employee=employee).first(), "group", None
        ),
        "shifts": [
            {
                "date": s.date.isoformat(),
                "start": s.start_min,
                "end": s.end_min,
                "pay_role": str(s.pay_role or ""),
                "note": s.note,
                "removed": s.deleted_at is not None,
            }
            for s in Shift.objects.filter(employee=employee).select_related("pay_role")
        ],
        "published_shifts": [
            {
                "version": v.number,
                "period_start": v.period.start_date.isoformat(),
                **{k: r[k] for k in ("date", "start", "end", "note")},
            }
            for v in PublishedVersion.objects.filter(organization=employee.organization).select_related(
                "period"
            )
            for r in v.shifts
            if r["employee_id"] == employee.pk
        ],
        "call_outs": [
            {
                "shift_date": c.shift.date.isoformat(),
                "role": "called out" if c.called_out_id == employee.pk else "covered",
                "note": c.note,
            }
            for c in CallOut.objects.filter(Q(called_out=employee) | Q(covered_by=employee)).select_related(
                "shift"
            )
        ],
        "shift_exchanges": [
            {
                "shift_date": x.shift.date.isoformat(),
                "kind": x.kind,
                "role": "requested" if x.requester_id == employee.pk else "took",
                "status": x.status,
            }
            for x in ShiftExchange.objects.filter(Q(requester=employee) | Q(taker=employee)).select_related(
                "shift"
            )
        ],
        "account": None,
    }
    if employee.user is not None:
        user = employee.user
        data["account"] = {
            "email": user.email,
            "date_joined": _plain(user.date_joined),
            "last_login": _plain(user.last_login),
            "notifications": [
                {"created_at": _plain(n.created_at), "subject": n.subject, "body": n.body}
                for n in Notification.objects.filter(user=user, organization=employee.organization)
            ],
            "email_settings": {
                p.category: p.email
                for p in EmailPreference.objects.filter(user=user, organization=employee.organization)
            },
        }
    return data


def erase_employee(employee: Employee) -> None:
    """Anonymise an archived employee and remove their personal data from the live records.

    The audit log and stored messages (validation messages, solver explanations, other
    people's notifications, import batches) are left as they are: they are the record of
    what happened (D100, D101). Notes about this person on their shifts and call-outs,
    including in published versions, are cleared."""
    from allauth.account.models import EmailAddress

    from app.availability.models import Availability, TimeOff
    from app.core.models import Membership
    from app.scheduling.models import (
        CalendarFeed,
        CallOut,
        DesiredHours,
        DutyAssignment,
        EmailPreference,
        HourCapRule,
        HourTargetRule,
        Notification,
        PublishedVersion,
        RotationAssignment,
        Shift,
    )

    if employee.archived_at is None:
        raise ErasureError("Archive the employee before erasing their data.")
    org = employee.organization
    pk = employee.pk
    with transaction.atomic(), pghistory.context(action="erase_employee", employee=pk):
        for model in (
            TimeOff,
            CalendarFeed,
            Availability,
            DesiredHours,
            HourCapRule,
            HourTargetRule,
            DutyAssignment,
            RotationAssignment,
        ):
            model.objects.filter(employee=employee).delete()
        employee.pay_roles.all().delete()
        employee.employee_tags.all().delete()
        employee.department_links.all().delete()
        CallOut.objects.filter(called_out=employee).update(note="")
        Shift.objects.filter(employee=employee).update(note="")

        user = employee.user
        employee.full_name = f"Erased employee {pk}"
        employee.preferred_name = "Erased"
        employee.email = ""
        employee.campus_id = ""
        employee.notes = ""
        employee.default_wage = None
        employee.hire_date = None
        employee.new_employee_until = None
        employee.user = None
        employee.in_scheduling_pool = False
        employee.shift_lead_access = False
        employee.save()

        if user is not None:
            Notification.objects.filter(user=user, organization=org).delete()
            EmailPreference.objects.filter(user=user, organization=org).delete()
            Membership.objects.filter(user=user, organization=org).delete()
            if not Membership.objects.filter(user=user).exists():
                EmailAddress.objects.filter(user=user).delete()
                user.email = f"erased-{user.pk}@invalid"
                user.is_active = False
                user.set_unusable_password()
                user.save()

        # Published versions keep the shifts (hour history) but lose notes about this person.
        for version in PublishedVersion.objects.filter(organization=org):
            rows = [_without_note(r, pk) for r in version.shifts]
            if rows != version.shifts:
                PublishedVersion.objects.filter(pk=version.pk).update(shifts=rows)


def _without_note(row: dict[str, Any], pk: int) -> dict[str, Any]:
    if row.get("employee_id") == pk and row.get("note"):
        return {**row, "note": ""}
    return row
