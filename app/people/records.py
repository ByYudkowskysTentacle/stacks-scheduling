# SPDX-License-Identifier: AGPL-3.0-or-later
"""Single-person data export and erasure (CLAUDE.md §6, SPEC §11): for records requests and
offboarding.

Erasure anonymises the employee rather than deleting the row, so shifts, published versions
and hour totals stay consistent (CLAUDE.md §2.3). It removes their availability, wages, tags,
caps, duties, targets, desired hours and notes, closes their sign-in account, and scrubs the
same personal data from audit snapshots and import batches. The erasure itself is recorded in the
audit log as an update to the employee made under the `erase_employee` context.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pghistory
from django.apps import apps
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
    from app.availability.models import Availability
    from app.scheduling.models import (
        CallOut,
        DesiredHours,
        DutyAssignment,
        HourCapRule,
        HourTargetRule,
        Notification,
        PublishedVersion,
        RotationAssignment,
        Shift,
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
        }
    return data


def erase_employee(employee: Employee) -> None:
    """Anonymise an archived employee and remove their personal data everywhere Stacks keeps it."""
    from allauth.account.models import EmailAddress

    from app.availability.models import Availability
    from app.core.models import Membership
    from app.imports.models import ImportBatch
    from app.scheduling.models import (
        CallOut,
        DesiredHours,
        DutyAssignment,
        HourCapRule,
        HourTargetRule,
        Notification,
        RotationAssignment,
        Shift,
    )

    if employee.archived_at is None:
        raise ErasureError("Archive the employee before erasing their data.")
    org = employee.organization
    old_email = (employee.email or "").lower()
    names_before = (employee.full_name, employee.preferred_name)
    pk = employee.pk
    with transaction.atomic(), pghistory.context(action="erase_employee", employee=pk):
        availability_ids = list(Availability.objects.filter(employee=employee).values_list("pk", flat=True))
        desired_ids = list(DesiredHours.objects.filter(employee=employee).values_list("pk", flat=True))
        Availability.objects.filter(employee=employee).delete()
        DesiredHours.objects.filter(employee=employee).delete()
        employee.pay_roles.all().delete()
        employee.employee_tags.all().delete()
        for model in (HourCapRule, HourTargetRule, DutyAssignment, RotationAssignment):
            model.objects.filter(employee=employee).delete()
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
            Membership.objects.filter(user=user, organization=org).delete()
            if not Membership.objects.filter(user=user).exists():
                EmailAddress.objects.filter(user=user).delete()
                user.email = f"erased-{user.pk}@invalid"
                user.is_active = False
                user.set_unusable_password()
                user.save()

        # Published snapshots and solver results keep the shifts (hour history) but lose notes
        # about this person, and their name in stored messages and explanations (D79).
        pattern = _name_pattern(names_before)
        replacement = f"Erased employee {pk}"
        for app_label, name, fields in (
            ("scheduling", "PublishedVersion", ("shifts", "issues")),
            ("scheduling", "PublishedVersionEvent", ("shifts", "issues")),
            ("scheduling", "SolveRun", ("result", "progress", "undo")),
            ("scheduling", "SolveRunEvent", ("result", "progress", "undo")),
        ):
            model = apps.get_model(app_label, name)
            for obj in model.objects.filter(organization_id=org.pk):
                updates = {}
                for f in fields:
                    before = getattr(obj, f)
                    after = _redact(before, pattern, replacement, pk)
                    if after != before:
                        updates[f] = after
                if updates:
                    model.objects.filter(pk=obj.pk).update(**updates)
        if pattern is not None:
            for n in Notification.objects.filter(organization=org):
                body = pattern.sub(replacement, n.body)
                if body != n.body:
                    Notification.objects.filter(pk=n.pk).update(body=body)

        # Import batches keep parsed rows, the preview and undo data: drop this person's entries.
        names = {n.lower() for n in names_before if n}
        for model in (ImportBatch, apps.get_model("imports", "ImportBatchEvent")):
            for b in model.objects.filter(organization_id=org.pk):
                payload = _scrub(b.payload, old_email, names)
                plan = _scrub(b.plan, old_email, names)
                undo = _scrub_undo(b.undo, pk)
                if (payload, plan, undo) != (b.payload, b.plan, b.undo):
                    model.objects.filter(pk=b.pk).update(payload=payload, plan=plan, undo=undo)

        # Audit snapshots: redact personal fields and drop availability history (D78).
        EmployeeEvent = apps.get_model("people", "EmployeeEvent")
        EmployeeEvent.objects.filter(pgh_obj_id=pk).update(
            full_name=f"Erased employee {pk}",
            preferred_name="Erased",
            email="",
            campus_id="",
            notes="",
            default_wage=None,
            hire_date=None,
            new_employee_until=None,
        )
        apps.get_model("people", "EmployeePayRoleEvent").objects.filter(employee_id=pk).update(wage=None)
        apps.get_model("availability", "AvailabilityBlockEvent").objects.filter(
            availability_id__in=availability_ids
        ).delete()
        apps.get_model("availability", "AvailabilityEvent").objects.filter(
            pgh_obj_id__in=availability_ids
        ).delete()
        apps.get_model("scheduling", "DesiredHoursEvent").objects.filter(pgh_obj_id__in=desired_ids).delete()
        apps.get_model("scheduling", "ShiftEvent").objects.filter(employee_id=pk).update(note="")
        apps.get_model("scheduling", "CallOutEvent").objects.filter(called_out_id=pk).update(note="")


def _name_pattern(names: tuple[str, ...]) -> re.Pattern[str] | None:
    """Whole-word match of the person's names, longest first. A co-worker who shares a
    preferred name is redacted too in stored messages: over-redaction is the safe side."""
    words = sorted({n.strip() for n in names if n and n.strip()}, key=len, reverse=True)
    if not words:
        return None
    return re.compile(r"\b(?:" + "|".join(re.escape(w) for w in words) + r")\b")


def _redact(value: Any, pattern: re.Pattern[str] | None, replacement: str, pk: int) -> Any:
    """Blank notes on this person's snapshot rows and replace their name in stored text."""
    if isinstance(value, list):
        return [_redact(v, pattern, replacement, pk) for v in value]
    if isinstance(value, dict):
        out = {k: _redact(v, pattern, replacement, pk) for k, v in value.items()}
        if out.get("employee_id") == pk and out.get("note"):
            out["note"] = ""
        return out
    if isinstance(value, str) and pattern is not None:
        return pattern.sub(replacement, value)
    return value


def _mentions(row: dict[str, Any], email: str, names: set[str]) -> bool:
    if email and str(row.get("email", "")).lower() == email:
        return True
    return str(row.get("name", "")).lower() in names


def _scrub(value: Any, email: str, names: set[str]) -> Any:
    """Drop list items (rows) that name or email this person, at any depth."""
    if isinstance(value, list):
        return [
            _scrub(v, email, names) for v in value if not (isinstance(v, dict) and _mentions(v, email, names))
        ]
    if isinstance(value, dict):
        return {k: _scrub(v, email, names) for k, v in value.items()}
    return value


def _scrub_undo(undo: dict[str, Any], pk: int) -> dict[str, Any]:
    """Undo data is keyed by employee ID: availability before-state, roster before-values."""
    out = {k: v for k, v in undo.items() if k != str(pk)}
    if isinstance(out.get("updated"), dict):
        out["updated"] = {k: v for k, v in out["updated"].items() if k != str(pk)}
    return out
