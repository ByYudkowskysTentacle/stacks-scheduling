# SPDX-License-Identifier: AGPL-3.0-or-later
"""Supervisors see and manage only their departments; admins see everything (D95, CLAUDE.md
§6). Every check here is server-side. All data is synthetic."""

from __future__ import annotations

from datetime import date
from unittest import mock

import pytest
from django.urls import reverse

from app.availability.models import TimeOff, TimeOffStatus
from app.core.models import Department, Role, Term
from app.people.models import Employee, EmployeeDepartment
from app.scheduling.models import (
    ExchangeKind,
    ExchangeStatus,
    SchedulePeriod,
    Shift,
    ShiftExchange,
)
from tests.app.test_foundations import employee_post_data
from tests.conftest import make_user, manages

pytestmark = pytest.mark.django_db
START = date(2027, 1, 16)
MONDAY = date(2027, 1, 18)


@pytest.fixture
def world(org, location, supervisor, pay_role):
    """Circulation and Special Collections; the supervisor manages Circulation only."""
    Term.objects.create(
        organization=org, name="Spring 2027", start_date=date(2027, 1, 11), end_date=date(2027, 5, 7)
    )
    circ = Department.objects.create(organization=org, name="Circulation")
    special = Department.objects.create(organization=org, name="Special Collections")
    manages(supervisor, circ)

    def person(name, *depts):
        e = Employee.objects.create(
            organization=org, full_name=f"{name} Sample", preferred_name=name, default_pay_role=pay_role
        )
        for d in depts:
            EmployeeDepartment.objects.create(organization=org, employee=e, department=d)
        return e

    people = {
        "Avery": person("Avery", circ),
        "Blake": person("Blake", special),
        "Casey": person("Casey", circ, special),
    }

    def period(dept):
        return SchedulePeriod.objects.create(
            organization=org, location=location, department=dept, start_date=START, weeks=2
        )

    periods = {"circ": period(circ), "special": period(special)}

    def shift(p, who, start=540, end=720):
        return Shift.objects.create(
            organization=org,
            location=location,
            period=p,
            employee=who,
            date=MONDAY,
            start_min=start,
            end_min=end,
            pay_role=pay_role,
        )

    shifts = {
        "circ": shift(periods["circ"], people["Avery"]),
        "special": shift(periods["special"], people["Blake"]),
    }
    return {"circ": circ, "special": special, "people": people, "periods": periods, "shifts": shifts}


def test_people_list_and_records_cover_my_departments_only(as_user, supervisor, world):
    client = as_user(supervisor)
    page = client.get(reverse("employees")).content.decode()
    assert "Avery" in page and "Casey" in page and "Blake" not in page
    blake = world["people"]["Blake"]
    for name in ("employee_detail", "employee_edit", "employee_export", "employee_archive"):
        assert client.get(reverse(name, args=[blake.pk])).status_code == 404, name
    for name in ("employee_invite", "employee_archive", "employee_edit"):
        assert client.post(reverse(name, args=[blake.pk]), {"confirm": "yes"}).status_code == 404, name
    blake.refresh_from_db()
    assert blake.archived_at is None


def test_admins_see_every_department(as_user, admin_user, world):
    client = as_user(admin_user)
    assert "Blake" in client.get(reverse("employees")).content.decode()
    assert client.get(reverse("period", args=[world["periods"]["special"].pk])).status_code == 200


def test_schedules_cover_my_departments_only(as_user, supervisor, world):
    client = as_user(supervisor)
    special = world["periods"]["special"]
    index = client.get(reverse("schedule_index")).content.decode()
    assert reverse("period", args=[world["periods"]["circ"].pk]) in index
    assert reverse("period", args=[special.pk]) not in index
    assert client.get(reverse("period", args=[special.pk])).status_code == 404
    shift = world["shifts"]["special"]
    assert client.get(reverse("shift_edit", args=[special.pk, shift.pk])).status_code == 404
    assert client.post(reverse("shift_edit", args=[special.pk, shift.pk]), {}).status_code == 404


def test_new_periods_only_in_my_departments(as_user, supervisor, world):
    client = as_user(supervisor)
    page = client.get(reverse("schedule_index")).content.decode()
    assert "Special Collections" not in page
    client.post(
        reverse("schedule_index"), {"department": world["special"].pk, "start_date": "2027-02-01", "weeks": 2}
    )
    assert SchedulePeriod.objects.count() == 2  # refused


def test_editing_someone_keeps_their_other_departments(as_user, supervisor, world):
    casey = world["people"]["Casey"]
    client = as_user(supervisor)
    page = client.get(reverse("employee_edit", args=[casey.pk])).content.decode()
    assert "Special Collections" not in page  # I can't assign what I don't manage
    client.post(
        reverse("employee_edit", args=[casey.pk]),
        employee_post_data(
            full_name="Casey Sample", preferred_name="Casey", email="", department_choices=[world["circ"].pk]
        ),
    )
    assert set(casey.departments.all()) == {world["circ"], world["special"]}


def test_a_supervisor_must_put_new_people_in_one_of_their_departments(as_user, supervisor, world):
    resp = as_user(supervisor).post(reverse("employee_add"), employee_post_data())
    assert resp.status_code == 200 and not Employee.objects.filter(preferred_name="Devon").exists()
    as_user(supervisor).post(
        reverse("employee_add"), employee_post_data(department_choices=[world["circ"].pk])
    )
    assert list(Employee.objects.get(preferred_name="Devon").departments.all()) == [world["circ"]]


def test_time_off_review_covers_my_departments_only(as_user, supervisor, org, world):
    blake = world["people"]["Blake"]
    t = TimeOff.objects.create(organization=org, employee=blake, start_date=MONDAY, end_date=MONDAY)
    client = as_user(supervisor)
    assert "Blake" not in client.get(reverse("time_off_review")).content.decode()
    resp = client.post(reverse("time_off_review"), {"request": t.pk, "action": "approve"})
    t.refresh_from_db()
    assert resp.status_code == 404 and t.status == TimeOffStatus.PENDING


def test_availability_grid_covers_my_departments_only(as_user, supervisor, world):
    page = as_user(supervisor).get(reverse("availability_grid")).content.decode()
    assert "Avery" in page and "Blake" not in page


def test_a_swap_across_departments_needs_a_supervisor_of_both(as_user, supervisor, admin_user, org, world):
    ex = ShiftExchange.objects.create(
        organization=org,
        shift=world["shifts"]["circ"],
        other_shift=world["shifts"]["special"],
        kind=ExchangeKind.SWAP,
        status=ExchangeStatus.PENDING,
        requester=world["people"]["Avery"],
        taker=world["people"]["Blake"],
    )
    client = as_user(supervisor)
    assert "Swap:" in client.get(reverse("review_queue")).content.decode()
    resp = client.post(
        reverse("review_queue"), {"action": "approve_exchange", "exchange": ex.pk}, follow=True
    )
    ex.refresh_from_db()
    assert ex.status == ExchangeStatus.PENDING and "A supervisor of both decides it" in resp.content.decode()
    client.post(reverse("review_queue"), {"action": "deny_exchange", "exchange": ex.pk})
    ex.refresh_from_db()
    assert ex.status == ExchangeStatus.DENIED


def test_a_supervisor_without_departments_sees_no_people_or_schedules(as_user, org, world):
    newcomer = make_user(org, "newcomer@example.edu", Role.SUPERVISOR)
    client = as_user(newcomer)
    page = client.get(reverse("employees")).content.decode()
    assert not any(name in page for name in ("Avery", "Blake", "Casey"))
    assert client.get(reverse("period", args=[world["periods"]["circ"].pk])).status_code == 404


def test_shift_leads_work_within_their_own_departments(as_user, org, world):
    avery = world["people"]["Avery"]
    avery.user = make_user(org, "avery-lead@example.edu", Role.EMPLOYEE)
    avery.shift_lead_access = True
    avery.save()
    client = as_user(avery.user)
    assert client.get(reverse("period", args=[world["periods"]["circ"].pk])).status_code == 200
    assert client.get(reverse("period", args=[world["periods"]["special"].pk])).status_code == 404


def test_remove_from_pool_leaves_other_departments_schedules_alone(
    as_user, supervisor, location, pay_role, world
):
    casey = world["people"]["Casey"]
    theirs = Shift.objects.create(
        organization=casey.organization,
        location=location,
        period=world["periods"]["special"],
        employee=casey,
        date=MONDAY,
        start_min=780,
        end_min=900,
        pay_role=pay_role,
    )
    with mock.patch("django.utils.timezone.localdate", return_value=START):
        as_user(supervisor).post(reverse("remove_from_pool", args=[casey.pk]), {"confirm": "yes"})
    theirs.refresh_from_db()
    assert theirs.employee == casey  # Special Collections' supervisor decides that one


# --- The audit log (D107) -------------------------------------------------------------------


def test_audit_log_shows_my_departments_and_settings_only(as_user, supervisor, admin_user, org, world):
    from decimal import Decimal

    for name in ("Avery", "Blake"):
        e = world["people"][name]
        e.default_wage = Decimal("15.75") if name == "Blake" else Decimal("14.25")
        e.save()
    world["circ"].name = "Circulation Desk"
    world["circ"].save()
    page = as_user(supervisor).get(reverse("audit_log")).content.decode()
    assert "Avery" in page and "14.25" in page
    assert "Blake" not in page and "15.75" not in page  # Special Collections' student
    assert "Circulation Desk" in page  # organization-wide settings stay visible
    admin_page = as_user(admin_user).get(reverse("audit_log")).content.decode()
    assert "Blake" in admin_page and "15.75" in admin_page


def test_every_audited_model_is_classified_for_the_audit_log():
    from django.apps import apps as registry
    from pghistory.models import Event

    from app.core import audit_log

    classified = (
        audit_log.ORGANIZATION_WIDE
        | audit_log.BY_EMPLOYEE
        | audit_log.BY_PERIOD
        | audit_log.BY_DEPARTMENT
        | {
            "people.Employee",
            "scheduling.HourCapRule",
            "availability.AvailabilityBlock",
            "scheduling.SchedulePeriod",
            "scheduling.CallOut",
            "scheduling.ShiftExchange",
            "scheduling.EmailPreference",
        }
    )
    tracked = {m.pgh_tracked_model._meta.label for m in registry.get_models() if issubclass(m, Event)}
    assert tracked <= classified, f"decide who sees the history of: {sorted(tracked - classified)}"
