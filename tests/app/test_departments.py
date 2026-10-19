# SPDX-License-Identifier: AGPL-3.0-or-later
"""Departments (D95): their own schedules and rosters, people in several, supervisors assigned
by an admin. All data is synthetic."""

from __future__ import annotations

import importlib
from datetime import date

import pytest
from django.apps import apps
from django.urls import reverse

from app.core.models import Department, Membership, Role, SupervisorDepartment
from app.people.models import Employee, EmployeeDepartment
from app.scheduling import services
from app.scheduling.models import SchedulePeriod, Shift
from tests.app.test_foundations import employee_post_data
from tests.conftest import make_user

pytestmark = pytest.mark.django_db
START = date(2027, 1, 16)
MONDAY = date(2027, 1, 18)


@pytest.fixture
def depts(org):
    return (
        Department.objects.create(organization=org, name="Circulation"),
        Department.objects.create(organization=org, name="Special Collections"),
    )


def person(org, name, *departments):
    e = Employee.objects.create(organization=org, full_name=f"{name} Sample", preferred_name=name)
    for d in departments:
        EmployeeDepartment.objects.create(organization=org, employee=e, department=d)
    return e


def test_the_upgrade_puts_everything_in_one_department(org, location, supervisor, employee):
    period = SchedulePeriod.objects.create(organization=org, location=location, start_date=START, weeks=2)
    migration = importlib.import_module("app.scheduling.migrations.0007_departments")
    migration.default_departments(apps, None)
    dept = Department.objects.get(organization=org)
    period.refresh_from_db()
    assert dept.name == "General" and period.department == dept
    assert list(employee.departments.all()) == [dept]
    assert list(Membership.objects.get(user=supervisor).departments.all()) == [dept]


def test_a_period_lists_only_its_departments_people(org, location, depts):
    circ, special = depts
    both = person(org, "Avery", circ, special)
    person(org, "Blake", special)
    guest = person(org, "Casey", special)
    period = SchedulePeriod.objects.create(
        organization=org, location=location, department=circ, start_date=START, weeks=2
    )
    Shift.objects.create(
        organization=org,
        location=location,
        period=period,
        employee=guest,
        date=MONDAY,
        start_min=540,
        end_min=720,
    )
    names = {p.name for p in services.build_context(period).people.values()}
    assert names == {"Avery", "Casey"}  # Casey is in another department but holds a shift here
    assert both.pk in {e.pk for e in services.employees_for_grid(period)}


def test_new_employees_join_the_only_department(org):
    dept = Department.objects.create(organization=org, name="General")
    assert list(person(org, "Devon").departments.all()) == [dept]


def test_with_several_departments_nobody_is_added_by_guesswork(org, depts):
    assert not person(org, "Devon").departments.exists()


def test_supervisor_puts_someone_in_departments_from_their_record(as_user, org, supervisor, depts):
    circ, special = depts
    client = as_user(supervisor)
    client.post(reverse("employee_add"), employee_post_data(department_choices=[circ.pk, special.pk]))
    devon = Employee.objects.get(preferred_name="Devon")
    assert set(devon.departments.all()) == {circ, special}
    client.post(
        reverse("employee_edit", args=[devon.pk]),
        employee_post_data(email=devon.email, department_choices=[special.pk]),
    )
    assert list(devon.departments.all()) == [special]
    assert "Special Collections" in client.get(reverse("employee_detail", args=[devon.pk])).content.decode()


def test_departments_are_added_and_renamed_in_settings(as_user, org, supervisor):
    client = as_user(supervisor)
    client.post(reverse("department_add"), {"name": "Circulation"})
    resp = client.post(reverse("department_add"), {"name": "circulation"})
    assert "already a department with that name" in resp.content.decode()
    dept = Department.objects.get()
    client.post(reverse("department_edit", args=[dept.pk]), {"name": "Access Services"})
    dept.refresh_from_db()
    assert dept.name == "Access Services"
    assert "Access Services" in client.get(reverse("departments")).content.decode()


def test_students_cannot_manage_departments(as_user, student_user, depts):
    client = as_user(student_user)
    assert client.get(reverse("departments")).status_code == 403
    assert client.post(reverse("department_add"), {"name": "Sneaky"}).status_code == 403


def test_departments_stay_inside_their_organization(as_user, other_org, supervisor):
    elsewhere = Department.objects.create(organization=other_org, name="Theirs")
    assert as_user(supervisor).get(reverse("department_edit", args=[elsewhere.pk])).status_code == 404


def test_admin_assigns_a_supervisor_their_departments(as_user, admin_user, supervisor, depts):
    circ, _ = depts
    member = Membership.objects.get(user=supervisor)
    client = as_user(admin_user)
    assert "Special Collections" in client.get(reverse("members")).content.decode()
    client.post(
        reverse("members"), {"membership": member.pk, "action": "departments", "department": [circ.pk]}
    )
    assert list(member.departments.all()) == [circ]
    client.post(reverse("members"), {"membership": member.pk, "action": "departments"})
    assert not member.departments.exists()


def test_a_new_supervisor_gets_the_only_department(as_user, org, admin_user):
    dept = Department.objects.create(organization=org, name="General")
    user = make_user(org, "new-supervisor@example.edu", Role.EMPLOYEE)
    member = Membership.objects.get(user=user)
    as_user(admin_user).post(reverse("members"), {"membership": member.pk, "role": Role.SUPERVISOR})
    assert SupervisorDepartment.objects.filter(membership=member, department=dept).exists()


def test_supervisors_cannot_assign_departments(as_user, supervisor, depts):
    member = Membership.objects.get(user=supervisor)
    resp = as_user(supervisor).post(
        reverse("members"), {"membership": member.pk, "action": "departments", "department": [depts[1].pk]}
    )
    assert resp.status_code == 403 and not member.departments.exists()
