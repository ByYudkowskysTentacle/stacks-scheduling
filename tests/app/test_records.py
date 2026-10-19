# SPDX-License-Identifier: AGPL-3.0-or-later
"""Single-person export and erasure (CLAUDE.md §6), and the hours and coverage reports
(SPEC §14). All data is synthetic."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from app.availability.models import Availability, AvailabilityBlock
from app.core.models import Department, Membership, Role, Term
from app.imports.models import BatchKind, ImportBatch
from app.people.models import Employee, EmployeeDepartment, EmployeePayRole, EmployeeTag, Tag
from app.people.records import ErasureError, erase_employee, export_employee
from app.scheduling import services
from app.scheduling.models import (
    DesiredHours,
    DesiredStatus,
    EmailPreference,
    HourCapRule,
    PublishedVersion,
    SchedulePeriod,
    Shift,
    SolveRun,
)
from tests.conftest import make_user

pytestmark = pytest.mark.django_db
START = date(2027, 1, 16)
MONDAY = date(2027, 1, 18)
SECRET_NOTE = "Avery has a dentist appointment"


@pytest.fixture
def records_setup(org, location, supervisor, student_user, employee, pay_role):
    term = Term.objects.create(
        organization=org, name="Spring 2027", start_date=date(2027, 1, 11), end_date=date(2027, 5, 7)
    )
    employee.campus_id = "S-0001"
    employee.notes = "Supervisor-only note about Avery"
    employee.default_wage = Decimal("13.50")
    employee.save()
    EmailPreference.objects.create(organization=org, user=student_user, category="time_off", email=True)
    EmployeePayRole.objects.create(
        organization=org, employee=employee, pay_role=pay_role, wage=Decimal("14.00")
    )
    EmployeeTag.objects.create(
        organization=org, employee=employee, tag=Tag.objects.create(organization=org, name="Opener")
    )
    avail = Availability.objects.create(organization=org, employee=employee, term=term)
    AvailabilityBlock.objects.create(
        organization=org, availability=avail, weekday=0, kind="unavailable", start_min=600, end_min=720
    )
    DesiredHours.objects.create(
        organization=org, employee=employee, term=term, minutes=600, status=DesiredStatus.APPROVED
    )
    HourCapRule.objects.create(organization=org, employee=employee, scope="week", minutes=600)
    period = SchedulePeriod.objects.create(organization=org, location=location, start_date=START, weeks=2)
    shift = Shift.objects.create(
        organization=org,
        location=location,
        period=period,
        employee=employee,
        date=MONDAY,
        start_min=540,
        end_min=720,
        pay_role=pay_role,
        note=SECRET_NOTE,
    )
    services.publish(period, supervisor)
    SolveRun.objects.create(
        organization=org,
        period=period,
        mode="rebalance",
        result={"options": [{"explanations": ["Avery Example gains 2h: limited availability"]}]},
    )
    ImportBatch.objects.create(
        organization=org,
        kind=BatchKind.ROSTER,
        payload=[{"row": 2, "email": "avery@example.edu", "full_name": "Avery Example"}],
        plan={"create": [{"row": 2, "name": "Avery", "email": "avery@example.edu"}], "update": []},
    )
    return {"term": term, "period": period, "shift": shift}


def _rows_mentioning(text: str) -> dict[str, int]:
    """Every table in the database whose rows contain `text`, as row text (case-insensitive)."""
    found = {}
    with connection.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        for (table,) in cur.fetchall():
            quoted = connection.ops.quote_name(table)  # from pg_tables, not user input
            cur.execute(f"SELECT count(*) FROM {quoted} t WHERE t::text ILIKE %s", [f"%{text}%"])  # noqa: S608
            (n,) = cur.fetchone()
            if n:
                found[table] = n
    return found


def _archive(employee):
    employee.archived_at = timezone.now()
    employee.in_scheduling_pool = False
    employee.save()


# --- Export ---------------------------------------------------------------------------------


def in_circulation(employee):
    dept = Department.objects.create(organization=employee.organization, name="Circulation")
    EmployeeDepartment.objects.create(organization=employee.organization, employee=employee, department=dept)


def test_export_holds_the_whole_record(records_setup, employee):
    in_circulation(employee)
    data = export_employee(employee)
    assert data["employee"]["full_name"] == "Avery Example"
    assert data["employee"]["default_wage"] == "13.50"
    assert data["pay_role_rates"][0]["wage"] == "14.00"
    assert data["tags"][0]["tag"] == "Opener"
    assert data["departments"] == ["Circulation"]
    assert data["availability"][0]["blocks"][0]["start"] == 600
    assert data["desired_hours"][0]["hours"] == 10
    assert data["shifts"][0]["note"] == SECRET_NOTE
    assert data["published_shifts"][0]["version"] == 1
    assert data["account"]["email"] == "avery@example.edu"
    assert data["account"]["email_settings"] == {"time_off": True}
    json.dumps(data)  # JSON-ready


def test_supervisor_downloads_the_export(as_user, supervisor, records_setup, employee):
    resp = as_user(supervisor).get(reverse("employee_export", args=[employee.pk]))
    assert resp.status_code == 200
    assert resp["Content-Disposition"].startswith("attachment")
    assert json.loads(resp.content)["employee"]["email"] == "avery@example.edu"


def test_students_and_leads_cannot_export(as_user, student_user, lead_user, records_setup, employee):
    for user in (student_user, lead_user):
        assert as_user(user).get(reverse("employee_export", args=[employee.pk])).status_code == 403


def test_export_is_scoped_to_the_organization(as_user, other_org, records_setup, employee):

    outsider = make_user(other_org, "outsider@example.edu", Role.ADMIN)
    assert as_user(outsider).get(reverse("employee_export", args=[employee.pk])).status_code == 404


# --- Erasure --------------------------------------------------------------------------------


def test_erasure_needs_the_employee_archived_first(records_setup, employee):
    with pytest.raises(ErasureError):
        erase_employee(employee)


# Stored messages that name people and are kept as the record (D101).
STORED_MESSAGES = {"scheduling_publishedversion", "scheduling_solverun", "imports_importbatch"}


def _live(found: dict[str, int]) -> dict[str, int]:
    """Drop audit-log tables (kept intact, D100) and pghistory's context table."""
    return {t: n for t, n in found.items() if not t.endswith("event") and t != "pghistory_context"}


def test_erasure_clears_personal_data_from_live_records(records_setup, employee, student_user):
    _archive(employee)
    assert _live(_rows_mentioning("avery"))  # sanity: the setup really did store it
    erase_employee(employee)
    for text in ("S-0001", "dentist", "Supervisor-only note"):
        assert _live(_rows_mentioning(text)) == {}, text
    left = set(_live(_rows_mentioning("avery")))
    assert left <= STORED_MESSAGES, left - STORED_MESSAGES


def test_erasure_keeps_the_audit_log_and_stored_messages(records_setup, employee):
    _archive(employee)
    erase_employee(employee)
    names = set(
        Employee.pgh_event_model.objects.filter(pgh_obj_id=employee.pk).values_list("full_name", flat=True)
    )
    assert "Avery Example" in names  # earlier snapshots are not rewritten (D100)
    assert "Avery Example" in str(SolveRun.objects.get().result)  # stored messages untouched (D101)
    assert ImportBatch.objects.get().payload[0]["email"] == "avery@example.edu"


def test_erasure_removes_personal_records_and_keeps_hours_history(records_setup, employee, student_user):
    in_circulation(employee)
    _archive(employee)
    pk = employee.pk
    erase_employee(employee)
    employee.refresh_from_db()
    assert employee.full_name == f"Erased employee {pk}"
    assert employee.default_wage is None and employee.user is None
    assert not Availability.objects.filter(employee=employee).exists()
    assert not DesiredHours.objects.filter(employee=employee).exists()
    assert not EmployeePayRole.objects.filter(employee=employee).exists()
    assert not EmployeeTag.objects.filter(employee=employee).exists()
    assert not EmployeeDepartment.objects.filter(employee=employee).exists()
    assert not HourCapRule.objects.filter(employee=employee).exists()
    # Shifts and published versions survive, without notes.
    shift = Shift.objects.get(pk=records_setup["shift"].pk)
    assert shift.employee_id == pk and shift.note == ""
    version = PublishedVersion.objects.get(period=records_setup["period"])
    assert [(r["employee_id"], r["start"], r["end"], r["note"]) for r in version.shifts] == [
        (pk, 540, 720, "")
    ]
    # The sign-in account is closed.
    student_user.refresh_from_db()
    assert not student_user.is_active and not student_user.has_usable_password()
    assert not Membership.objects.filter(user=student_user).exists()
    assert not EmailPreference.objects.filter(user=student_user).exists()


def test_erasure_is_recorded_in_the_audit_log(records_setup, employee):
    _archive(employee)
    erase_employee(employee)
    event = Employee.pgh_event_model.objects.filter(pgh_obj_id=employee.pk).order_by("-pgh_id").first()
    assert event.pgh_context.metadata["action"] == "erase_employee"


def test_only_admins_erase_and_only_with_the_typed_name(
    as_user, admin_user, supervisor, records_setup, employee
):
    _archive(employee)
    url = reverse("employee_erase", args=[employee.pk])
    assert as_user(supervisor).get(url).status_code == 403
    client = as_user(admin_user)
    assert client.get(url).status_code == 200
    assert client.post(url, {"confirm_name": "Avery"}).status_code == 400
    employee.refresh_from_db()
    assert employee.full_name == "Avery Example"
    resp = client.post(url, {"confirm_name": "Avery Example"})
    assert resp.status_code == 302
    employee.refresh_from_db()
    assert employee.full_name.startswith("Erased employee")


def test_current_employees_cannot_be_erased_from_the_web(as_user, admin_user, records_setup, employee):
    assert as_user(admin_user).get(reverse("employee_erase", args=[employee.pk])).status_code == 404


def test_a_coworker_with_another_name_is_untouched(org, records_setup, employee):
    other = Employee.objects.create(organization=org, full_name="Casey Other", preferred_name="Casey")
    _archive(employee)
    erase_employee(employee)
    other.refresh_from_db()
    assert other.full_name == "Casey Other"


# --- Reports --------------------------------------------------------------------------------


def test_hours_report_and_exports(as_user, supervisor, records_setup):
    period = records_setup["period"]
    client = as_user(supervisor)
    url = reverse("reports", args=[period.pk])
    page = client.get(url)
    assert page.status_code == 200
    assert b"Avery" in page.content
    csv_resp = client.get(url, {"format": "csv", "report": "hours"})
    assert csv_resp.status_code == 200
    header = csv_resp.content.decode().splitlines()[0]
    assert "weeks_1" in header and "period_hours" in header
    js = json.loads(client.get(url, {"format": "json", "report": "coverage"}).content)
    assert js["report"] == "coverage" and len(js["rows"]) == 14
    assert "drafts" in js["source"]


def test_leads_see_hours_but_cannot_download_reports(as_user, lead_user, student_user, records_setup):
    url = reverse("reports", args=[records_setup["period"].pk])
    lead = as_user(lead_user)
    page = lead.get(url)
    assert page.status_code == 200
    assert b"Desired" not in page.content
    assert lead.get(url, {"format": "csv", "report": "hours"}).status_code == 403
    assert as_user(student_user).get(url).status_code == 403
