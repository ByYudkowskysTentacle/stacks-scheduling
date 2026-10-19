# SPDX-License-Identifier: AGPL-3.0-or-later
"""Plan, apply and undo roster and availability imports (D13, SPEC §5.2, §9.3).

Each import is previewed first. Applying runs in one transaction under one audit
context, so every row change is grouped under the batch; undo restores the
before-state recorded at apply time.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

import pghistory
from django.db import transaction
from django.db.models import ProtectedError
from django.utils import timezone

from adapters.csv_formats import AvailabilityRow, ParseResult, RosterRow, parse_availability, parse_roster
from app.availability.models import Availability, AvailabilityBlock, Status
from app.core.models import Organization, Term
from app.imports.models import BatchKind, BatchStatus, ImportBatch
from app.people.models import Employee, EmployeePayRole, EmployeeTag, PayRole, Tag
from domain.availability import AvailabilityBlock as DomBlock
from domain.availability import BlockKind, normalise_blocks
from domain.timeutil import Interval

MAX_UPLOAD_BYTES = 2 * 1024 * 1024


class ImportProblem(Exception):
    """A problem that stops an import, with a message safe to show the supervisor."""


def decode_upload(data: bytes) -> str:
    if len(data) > MAX_UPLOAD_BYTES:
        raise ImportProblem("The file is larger than 2 MB. Split it into smaller files.")
    for encoding in ("utf-8-sig", "cp1252"):  # Excel on Windows often saves CSV as cp1252
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ImportProblem("The file isn't readable text. Save it as CSV (UTF-8).")


# --- Roster -------------------------------------------------------------------------------


def _roster_row_json(r: RosterRow) -> dict[str, Any]:
    return {
        "row": r.row,
        "full_name": r.full_name,
        "preferred_name": r.preferred_name,
        "email": r.email,
        "campus_id": r.campus_id,
        "employment_type": r.employment_type,
        "hire_date": r.hire_date.isoformat() if r.hire_date else None,
        "new_employee_until": r.new_employee_until.isoformat() if r.new_employee_until else None,
        "default_pay_role": r.default_pay_role,
        "tags": list(r.tags),
    }


def _match(org: Organization, email: str) -> Employee | None:
    return Employee.objects.filter(organization=org, email__iexact=email).first()


def _wanted_fields(row: dict[str, Any], existing: Employee | None) -> dict[str, Any]:
    """Field values the row sets. Blank optional cells leave an existing value alone."""
    out: dict[str, Any] = {
        "full_name": row["full_name"],
        "preferred_name": row["preferred_name"],
        "email": row["email"],
        "employment_type": row["employment_type"],
    }
    if row["campus_id"] or existing is None:
        out["campus_id"] = row["campus_id"]
    for name in ("hire_date", "new_employee_until"):
        if row[name] or existing is None:
            out[name] = date.fromisoformat(row[name]) if row[name] else None
    return out


def plan_roster(
    org: Organization, result: ParseResult[RosterRow]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload = [_roster_row_json(r) for r in result.rows]
    problems = [str(p) for p in result.problems]
    tags = set(Tag.objects.filter(organization=org).values_list("name", flat=True))
    roles = set(PayRole.objects.filter(organization=org).values_list("name", flat=True))
    creates, updates, unchanged = [], [], 0
    new_tags: set[str] = set()
    new_roles: set[str] = set()
    for row in payload:
        existing = _match(org, row["email"])
        if row["campus_id"]:
            clash = Employee.objects.filter(organization=org, campus_id=row["campus_id"])
            if existing:
                clash = clash.exclude(pk=existing.pk)
            if clash.exists():
                problems.append(f"row {row['row']}, campus_id: already used by another employee")
        new_tags |= {t for t in row["tags"] if t not in tags}
        if row["default_pay_role"] and row["default_pay_role"] not in roles:
            new_roles.add(row["default_pay_role"])
        if existing is None:
            creates.append({"row": row["row"], "name": row["preferred_name"], "email": row["email"]})
            continue
        changes = {
            k: [_jsonable(getattr(existing, k)), _jsonable(v)]
            for k, v in _wanted_fields(row, existing).items()
            if getattr(existing, k) != v
        }
        held = {et.tag.name for et in existing.employee_tags.select_related("tag")}
        add_tags = sorted(t for t in row["tags"] if t not in held)
        current_role = existing.default_pay_role.name if existing.default_pay_role else ""
        if row["default_pay_role"] and current_role != row["default_pay_role"]:
            changes["default_pay_role"] = [current_role, row["default_pay_role"]]
        if changes or add_tags:
            updates.append(
                {
                    "row": row["row"],
                    "name": existing.display_name,
                    "changes": changes,
                    "add_tags": add_tags,
                    "archived": existing.is_archived,
                }
            )
        else:
            unchanged += 1
    plan = {
        "create": creates,
        "update": updates,
        "unchanged": unchanged,
        "new_tags": sorted(new_tags),
        "new_pay_roles": sorted(new_roles),
        "problems": problems,
        "warnings": [str(w) for w in result.warnings],
    }
    return payload, plan


def _jsonable(value: Any) -> Any:
    return value.isoformat() if isinstance(value, date) else value


def apply_roster(batch: ImportBatch) -> None:
    org = batch.organization
    undo: dict[str, Any] = {
        "created_employees": [],
        "created_tags": [],
        "created_pay_roles": [],
        "created_employee_tags": [],
        "created_employee_pay_roles": [],
        "updated": {},
    }
    tags = {t.name: t for t in Tag.objects.filter(organization=org)}
    roles = {r.name: r for r in PayRole.objects.filter(organization=org)}
    for row in batch.payload:
        existing = _match(org, row["email"])
        role = None
        if row["default_pay_role"]:
            role = roles.get(row["default_pay_role"])
            if role is None:
                role = roles[row["default_pay_role"]] = PayRole.objects.create(
                    organization=org, name=row["default_pay_role"]
                )
                undo["created_pay_roles"].append(role.pk)
        wanted = _wanted_fields(row, existing)
        if existing is None:
            emp = Employee.objects.create(organization=org, default_pay_role=role, **wanted)
            undo["created_employees"].append(emp.pk)
        else:
            emp = existing
            before = {k: _jsonable(getattr(emp, k)) for k in wanted if getattr(emp, k) != wanted[k]}
            if role is not None and emp.default_pay_role_id != role.pk:
                before["default_pay_role_id"] = emp.default_pay_role_id
                emp.default_pay_role = role
            for k, v in wanted.items():
                setattr(emp, k, v)
            if before:
                emp.save()
                undo["updated"][str(emp.pk)] = before
        if role is not None and not EmployeePayRole.objects.filter(employee=emp, pay_role=role).exists():
            epr = EmployeePayRole.objects.create(organization=org, employee=emp, pay_role=role)
            undo["created_employee_pay_roles"].append(epr.pk)
        held = set(emp.employee_tags.values_list("tag__name", flat=True))
        for name in row["tags"]:
            if name in held:
                continue
            tag = tags.get(name)
            if tag is None:
                tag = tags[name] = Tag.objects.create(organization=org, name=name)
                undo["created_tags"].append(tag.pk)
            et = EmployeeTag.objects.create(organization=org, employee=emp, tag=tag)
            undo["created_employee_tags"].append(et.pk)
    batch.undo = undo


def undo_roster(batch: ImportBatch) -> None:
    u = batch.undo
    EmployeeTag.objects.filter(pk__in=u["created_employee_tags"]).delete()
    EmployeePayRole.objects.filter(pk__in=u["created_employee_pay_roles"]).delete()
    for pk, before in u["updated"].items():
        emp = Employee.objects.get(pk=pk)
        for k, v in before.items():
            if k in ("hire_date", "new_employee_until") and v:
                v = date.fromisoformat(v)
            setattr(emp, k, v)
        emp.save()
    for emp in Employee.objects.filter(pk__in=u["created_employees"]):
        try:
            with transaction.atomic():
                emp.delete()
        except ProtectedError:  # referenced since the import (for example by a shift): archive instead
            emp.archived_at = timezone.now()
            emp.in_scheduling_pool = False
            emp.save()
    for model, key in ((Tag, "created_tags"), (PayRole, "created_pay_roles")):
        for obj in model.objects.filter(pk__in=u[key]):
            try:
                with transaction.atomic():
                    obj.delete()
            except ProtectedError:
                pass


# --- Availability ---------------------------------------------------------------------------


def plan_availability(
    org: Organization, term: Term, result: ParseResult[AvailabilityRow]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload = [
        {
            "row": r.row,
            "email": r.email,
            "weekday": r.weekday,
            "kind": str(r.kind),
            "start": r.interval.start,
            "end": r.interval.end,
        }
        for r in result.rows
    ]
    problems = [str(p) for p in result.problems]
    by_email: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in payload:
        by_email[row["email"]].append(row)
    people = []
    for email, rows in sorted(by_email.items()):
        emp = Employee.objects.filter(organization=org, email__iexact=email, archived_at__isnull=True).first()
        if emp is None:
            problems.append(
                f"row {rows[0]['row']}, email: no current employee has this email; import the roster first"
            )
            continue
        current = Availability.objects.filter(employee=emp, term=term).first()
        people.append(
            {
                "name": emp.display_name,
                "blocks": len(rows),
                "replaces": current.blocks.count() if current else 0,
                "previous_status": current.get_status_display() if current else "Not entered",
            }
        )
    plan = {
        "people": people,
        "problems": problems,
        "warnings": [str(w) for w in result.warnings],
        "term": term.name,
    }
    return payload, plan


def apply_availability(batch: ImportBatch) -> None:
    org, term = batch.organization, batch.term
    if term is None:
        raise ImportProblem("An availability import needs a term.")
    by_email: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in batch.payload:
        by_email[row["email"]].append(row)
    undo: dict[str, Any] = {}
    for email, rows in by_email.items():
        emp = Employee.objects.get(organization=org, email__iexact=email, archived_at__isnull=True)
        current = Availability.objects.filter(employee=emp, term=term).first()
        undo[str(emp.pk)] = (
            {
                "existed": True,
                "status": current.status,
                "blocks": [[b.weekday, b.kind, b.start_min, b.end_min] for b in current.blocks.all()],
            }
            if current
            else {"existed": False}
        )
        if current is None:
            current = Availability(organization=org, employee=emp, term=term)
        current.status = Status.NEEDS_REVIEW  # imported data may be from a previous term (D11)
        current.confirmed_by = None
        current.confirmed_at = None
        current.save()
        current.blocks.all().delete()
        blocks = normalise_blocks(
            DomBlock(r["weekday"], BlockKind(r["kind"]), Interval(r["start"], r["end"])) for r in rows
        )
        AvailabilityBlock.objects.bulk_create(
            AvailabilityBlock(
                organization=org,
                availability=current,
                weekday=b.weekday,
                kind=str(b.kind),
                start_min=b.interval.start,
                end_min=b.interval.end,
            )
            for b in blocks
        )
    batch.undo = undo


def undo_availability(batch: ImportBatch) -> None:
    for emp_pk, before in batch.undo.items():
        current = Availability.objects.filter(employee_id=int(emp_pk), term=batch.term).first()
        if current is None:
            continue
        if not before["existed"]:
            current.delete()
            continue
        current.status = before["status"]
        current.save()
        current.blocks.all().delete()
        AvailabilityBlock.objects.bulk_create(
            AvailabilityBlock(
                organization=batch.organization,
                availability=current,
                weekday=w,
                kind=k,
                start_min=s,
                end_min=e,
            )
            for w, k, s, e in before["blocks"]
        )


# --- Batches --------------------------------------------------------------------------------


def create_batch(
    org: Organization, user, kind: str, text: str, filename: str, term: Term | None
) -> ImportBatch:
    if kind == BatchKind.ROSTER:
        payload, plan = plan_roster(org, parse_roster(text))
    else:
        if term is None:
            raise ImportProblem("Choose the term this availability is for.")
        payload, plan = plan_availability(org, term, parse_availability(text))
    return ImportBatch.objects.create(
        organization=org,
        kind=kind,
        term=term,
        filename=filename[:200],
        created_by=user,
        payload=payload,
        plan=plan,
    )


def apply(batch: ImportBatch) -> None:
    if batch.status != BatchStatus.PREVIEW:
        raise ImportProblem("This import has already been applied or discarded.")
    if batch.plan.get("problems"):
        raise ImportProblem("Fix the problems in the file and upload it again.")
    with transaction.atomic(), pghistory.context(import_batch=batch.pk, action="apply"):
        (apply_roster if batch.kind == BatchKind.ROSTER else apply_availability)(batch)
        batch.status = BatchStatus.APPLIED
        batch.applied_at = timezone.now()
        batch.save()


def can_undo(batch: ImportBatch) -> bool:
    if batch.status != BatchStatus.APPLIED:
        return False
    later = ImportBatch.objects.filter(
        organization=batch.organization,
        kind=batch.kind,
        status=BatchStatus.APPLIED,
        applied_at__gt=batch.applied_at,
    )
    return not later.exists()


def undo(batch: ImportBatch) -> None:
    if not can_undo(batch):
        raise ImportProblem("Only the most recent applied import of this kind can be undone.")
    with transaction.atomic(), pghistory.context(import_batch=batch.pk, action="undo"):
        (undo_roster if batch.kind == BatchKind.ROSTER else undo_availability)(batch)
        batch.status = BatchStatus.UNDONE
        batch.undone_at = timezone.now()
        batch.save()
