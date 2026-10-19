# SPDX-License-Identifier: AGPL-3.0-or-later
"""Departments (D95): their own schedules and rosters, people in several, supervisors assigned
by an admin. All data is synthetic."""

from __future__ import annotations

import importlib
from datetime import date

import pytest
from django.apps import apps
from django.urls import reverse
from pghistory.models import Events

from app.core.models import Department, Membership, Role, SupervisorDepartment
from app.people.models import Employee, EmployeeDepartment, Tag
from app.scheduling import services
from app.scheduling.models import CoverageOverride, SchedulePeriod, Shift, TagCoverageRule
from tests.app.test_foundations import employee_post_data
from tests.conftest import make_user, manages

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
    migration = importlib.import_module("app.scheduling.migrations.0008_departments")
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
    manages(supervisor, circ, special)
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


# --- Per-department coverage (D95, part 3) --------------------------------------------------


def test_each_department_gets_its_own_coverage(org, location, depts):
    circ, special = depts
    special.coverage_default = 1
    special.coverage_mode = "minimum"
    special.save()
    tag = Tag.objects.create(organization=org, name="Cataloguer")
    for dept, count in ((None, 3), (circ, 4), (special, 5)):
        CoverageOverride.objects.create(
            organization=org,
            location=location,
            department=dept,
            weekdays=[0],
            start_min=600,
            end_min=720,
            count=count,
        )
    TagCoverageRule.objects.create(organization=org, tag=tag, kind="min_on_duty", department=circ)
    circ_rules = services.domain_rules(org, None, circ)
    special_rules = services.domain_rules(org, None, special)
    assert circ_rules.coverage.default == 2 and circ_rules.coverage.mode == "exact"  # the organization's
    assert special_rules.coverage.default == 1 and special_rules.coverage.mode == "minimum"
    assert sorted(o.count for o in circ_rules.coverage.overrides) == [3, 4]
    assert sorted(o.count for o in special_rules.coverage.overrides) == [3, 5]
    assert len(circ_rules.tag_rules) == 1 and special_rules.tag_rules == []


def test_a_periods_validation_uses_its_departments_coverage(org, location, depts):
    _, special = depts
    special.coverage_default = 0
    special.save()
    period = SchedulePeriod.objects.create(
        organization=org, location=location, department=special, start_date=START, weeks=2
    )
    assert not [i for i in services.validate_period(period).issues if i.category == "coverage_gap"]


def test_rules_page_asks_which_department_only_when_there_are_several(as_user, org, supervisor, location):
    client = as_user(supervisor)
    only = Department.objects.create(organization=org, name="General")
    assert "Every department" not in client.get(reverse("rules")).content.decode()
    special = Department.objects.create(organization=org, name="Special Collections")
    assert "Every department" in client.get(reverse("rules")).content.decode()
    client.post(
        reverse("rules"),
        {
            "form": "override",
            "department": special.pk,
            "weekdays": [0],
            "start_min": "10:00",
            "end_min": "12:00",
            "count": 1,
        },
    )
    assert CoverageOverride.objects.get().department == special
    assert only.coverage_overrides.count() == 0


def test_department_coverage_is_set_on_its_settings_page(as_user, org, supervisor):
    dept = Department.objects.create(organization=org, name="Special Collections")
    as_user(supervisor).post(
        reverse("department_edit", args=[dept.pk]),
        {"name": dept.name, "coverage_default": 1, "coverage_mode": "minimum"},
    )
    dept.refresh_from_db()
    assert (dept.coverage_default, dept.coverage_mode) == (1, "minimum")


# --- Removing a department (D110) ------------------------------------------------------------


def test_an_empty_department_is_removed_after_confirmation(as_user, org, admin_user, depts):
    circ, special = depts
    circ.open_shifts_from = [special.pk]
    circ.save()
    client = as_user(admin_user)
    url = reverse("department_remove", args=[special.pk])
    assert "no schedules, people, supervisors" in client.get(url).content.decode()
    client.post(url, {})
    assert Department.objects.filter(pk=special.pk).exists()  # not without the confirmation box
    resp = client.post(url, {"confirm": "on"}, follow=True)
    assert "Removed Special Collections." in resp.content.decode()
    assert "Circulation no longer shows its open shifts" in resp.content.decode()
    assert list(Department.objects.all()) == [circ]
    circ.refresh_from_db()
    assert circ.open_shifts_from == []
    event = Events.objects.filter(pgh_obj_model="core.Department", pgh_label="delete").get()
    assert event.pgh_context["user"] == admin_user.pk


def test_removing_merges_everything_into_the_chosen_department(
    as_user, org, location, admin_user, supervisor, depts
):
    circ, special = depts
    manages(supervisor, circ, special)
    both, only_special = person(org, "Avery", circ, special), person(org, "Blake", special)
    period = SchedulePeriod.objects.create(
        organization=org, location=location, department=special, start_date=START, weeks=2
    )
    shift = Shift.objects.create(
        organization=org,
        location=location,
        period=period,
        employee=only_special,
        date=MONDAY,
        start_min=540,
        end_min=720,
    )
    tag = Tag.objects.create(organization=org, name="Cataloguer")
    rule = TagCoverageRule.objects.create(organization=org, tag=tag, kind="min_on_duty", department=special)
    client = as_user(admin_user)
    url = reverse("department_remove", args=[special.pk])
    page = client.get(url).content.decode()
    assert "1 schedule," in page and "2 people" in page and "1 skill rule" in page
    resp = client.post(url, {"confirm": "on"})
    assert "This field is required." in resp.content.decode()  # a department to move into
    client.post(url, {"confirm": "on", "into": circ.pk})
    assert not Department.objects.filter(pk=special.pk).exists()
    period.refresh_from_db()
    rule.refresh_from_db()
    assert period.department == circ and rule.department == circ
    assert Shift.objects.get().pk == shift.pk  # same shift, same ID
    assert list(both.departments.all()) == [circ] and list(only_special.departments.all()) == [circ]
    assert list(Membership.objects.get(user=supervisor).departments.all()) == [circ]


def test_removal_is_refused_when_both_have_a_schedule_for_the_same_week(
    as_user, org, location, admin_user, depts
):
    circ, special = depts
    for dept in depts:
        SchedulePeriod.objects.create(organization=org, location=location, department=dept, start_date=START)
    resp = as_user(admin_user).post(
        reverse("department_remove", args=[special.pk]), {"confirm": "on", "into": circ.pk}
    )
    assert "already has a schedule for the same week" in resp.content.decode()
    assert Department.objects.count() == 2 and SchedulePeriod.objects.filter(department=special).exists()


def test_the_only_department_cannot_be_removed(as_user, org, admin_user):
    dept = Department.objects.create(organization=org, name="General")
    client = as_user(admin_user)
    url = reverse("department_remove", args=[dept.pk])
    assert "only department" in client.get(url).content.decode()
    assert "only department" in client.post(url, {"confirm": "on"}).content.decode()
    assert Department.objects.filter(pk=dept.pk).exists()


def test_only_admins_remove_departments(as_user, org, supervisor, depts):
    _, special = depts
    manages(supervisor, special)
    client = as_user(supervisor)
    assert client.post(reverse("department_remove", args=[special.pk]), {"confirm": "on"}).status_code == 403
    assert "Remove" not in client.get(reverse("departments")).content.decode()
    assert Department.objects.filter(pk=special.pk).exists()


def test_cannot_merge_into_another_organizations_department(as_user, other_org, admin_user, depts):
    _, special = depts
    person(special.organization, "Blake", special)
    theirs = Department.objects.create(organization=other_org, name="Theirs")
    resp = as_user(admin_user).post(
        reverse("department_remove", args=[special.pk]), {"confirm": "on", "into": theirs.pk}
    )
    assert "Select a valid choice" in resp.content.decode()
    assert Department.objects.filter(pk=special.pk).exists()
