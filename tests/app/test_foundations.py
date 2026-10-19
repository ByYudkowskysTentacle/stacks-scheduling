# SPDX-License-Identifier: AGPL-3.0-or-later
"""Milestone 1: authorization, organization scoping, configuration forms, people and audit."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from django.core import mail
from django.urls import reverse
from pghistory.models import Events

from app.core.forms import ClosureForm, parse_day_intervals, parse_time_input
from app.core.models import Closure, Membership, OperatingHoursSet, Role
from app.people.forms import EmployeeForm
from app.people.models import Employee, EmployeePayRole
from domain.timeutil import Interval
from tests.conftest import make_user

pytestmark = pytest.mark.django_db

SUPERVISOR_PAGES = [
    "employees",
    "employee_add",
    "locations",
    "terms",
    "pay_roles",
    "tags",
    "organization_settings",
    "audit_log",
]


# --- Authorization -----------------------------------------------------------------------


def test_anonymous_users_are_sent_to_sign_in(client):
    response = client.get(reverse("employees"))
    assert response.status_code == 302
    assert reverse("account_login") in response["Location"]


@pytest.mark.parametrize("page", SUPERVISOR_PAGES)
def test_supervisor_pages_allow_supervisors(as_user, supervisor, location, page):
    assert as_user(supervisor).get(reverse(page)).status_code == 200


@pytest.mark.parametrize("page", [*SUPERVISOR_PAGES, "members"])
def test_students_cannot_open_supervisor_pages(as_user, student_user, page):
    assert as_user(student_user).get(reverse(page)).status_code == 403


@pytest.mark.parametrize("page", [*SUPERVISOR_PAGES, "members"])
def test_shift_leads_cannot_open_supervisor_pages(as_user, lead_user, page):
    # D25: leads get scheduling helpers, never the roster, wages, notes or settings.
    assert as_user(lead_user).get(reverse(page)).status_code == 403


def test_only_admins_manage_roles(as_user, supervisor, admin_user):
    assert as_user(supervisor).get(reverse("members")).status_code == 403
    assert as_user(admin_user).get(reverse("members")).status_code == 200


def test_user_without_membership_is_refused(as_user, db):
    from app.accounts.models import User

    stranger = User.objects.create_user("stranger@example.edu", "x-long-password-1")
    assert as_user(stranger).get(reverse("home")).status_code == 403


def test_shift_lead_tag_does_not_grant_access(as_user, org, student_user, employee):
    from app.people.models import EmployeeTag, Tag

    tag = Tag.objects.create(organization=org, name="Shift lead")
    EmployeeTag.objects.create(organization=org, employee=employee, tag=tag)
    client = as_user(student_user)
    assert client.get(reverse("home")).context["access"].is_shift_lead is False


def test_public_sign_up_is_closed(client):
    response = client.get(reverse("account_signup"))
    assert b"Sign Up Closed" in response.content or response.status_code in (403, 404)


def test_sign_in_with_email_and_password(client, supervisor):
    response = client.post(
        reverse("account_login"), {"login": "supervisor@example.edu", "password": "a-long-test-password"}
    )
    assert response.status_code == 302
    assert client.get(reverse("home")).status_code == 200


# --- Organization scoping ------------------------------------------------------------------


def test_supervisor_cannot_see_another_organizations_employee(as_user, supervisor, other_org):
    outsider = Employee.objects.create(
        organization=other_org, full_name="Casey Other", preferred_name="Casey"
    )
    client = as_user(supervisor)
    assert client.get(reverse("employee_detail", args=[outsider.pk])).status_code == 404
    assert client.get(reverse("employee_edit", args=[outsider.pk])).status_code == 404
    assert b"Casey" not in client.get(reverse("employees")).content


def test_supervisor_cannot_edit_another_organizations_hours(as_user, supervisor, other_org):
    from app.core.models import Location

    loc = Location.objects.create(organization=other_org, name="Elsewhere")
    assert as_user(supervisor).get(reverse("hours_add", args=[loc.pk])).status_code == 404


# --- Operating hours and closures -----------------------------------------------------------


def test_time_input_parsing():
    assert parse_time_input("7:30") == 450
    assert parse_time_input("7:30pm") == 1170
    assert parse_time_input("12am") == 0
    assert parse_time_input("12pm") == 720
    assert parse_time_input("midnight") == 1440
    for bad in ("13pm", "7:75", "25:00", "abc"):
        with pytest.raises(ValueError):
            parse_time_input(bad)


def test_day_interval_parsing():
    assert parse_day_intervals("07:30-22:00") == [Interval(450, 1320)]
    assert parse_day_intervals("8am-12pm, 1pm to 5pm") == [Interval(480, 720), Interval(780, 1020)]
    assert parse_day_intervals("  ") == []
    for bad in ("22:00-07:30", "07:31-08:00", "8am"):
        with pytest.raises(ValueError):
            parse_day_intervals(bad)


def test_locations_page_shows_pilot_weekly_open_hours(as_user, supervisor, location):
    response = as_user(supervisor).get(reverse("locations"))
    assert b"73.5 open hours" in response.content


def test_create_operating_hours_from_text(as_user, supervisor, location):
    client = as_user(supervisor)
    data = {"name": "Finals", "effective_from": "2026-12-01", "effective_to": "2026-12-11"}
    data.update({f"day_{wd}": "" for wd in range(7)})
    data["day_0"] = "07:00-24:00"
    response = client.post(reverse("hours_add", args=[location.pk]), data)
    assert response.status_code == 302
    finals = OperatingHoursSet.objects.get(name="Finals")
    assert [(i.weekday, i.start_min, i.end_min) for i in finals.intervals.all()] == [(0, 420, 1440)]


def test_bad_operating_hours_are_rejected_with_a_message(as_user, supervisor, location):
    data = {
        "name": "Bad",
        "effective_from": "2026-12-01",
        **{f"day_{wd}": "" for wd in range(7)},
        "day_2": "9-8",
    }
    response = as_user(supervisor).post(reverse("hours_add", args=[location.pk]), data)
    assert response.status_code == 200
    assert "ends before it starts" in response.content.decode()
    assert not OperatingHoursSet.objects.filter(name="Bad").exists()


def test_closure_form_requires_weekday_or_date(location):
    form = ClosureForm(
        {"reason": "Chapel", "kind": Closure.RECURRING, "start_min": "10:00", "end_min": "11:00"},
        location=location,
    )
    assert not form.is_valid()
    assert "weekday" in form.errors
    form = ClosureForm(
        {"reason": "Holiday", "kind": Closure.DATED, "on_date": "2027-01-18"}, location=location
    )
    assert form.is_valid(), form.errors
    closure = form.save()
    assert closure.all_day and closure.to_domain().on == date(2027, 1, 18)


def test_closure_times_must_be_on_slot_and_ordered(location):
    form = ClosureForm(
        {"reason": "X", "kind": Closure.RECURRING, "weekday": 2, "start_min": "11:00", "end_min": "10:00"},
        location=location,
    )
    assert not form.is_valid()
    form = ClosureForm(
        {"reason": "X", "kind": Closure.RECURRING, "weekday": 2, "start_min": "10:10", "end_min": "11:00"},
        location=location,
    )
    assert not form.is_valid()


# --- People ---------------------------------------------------------------------------------


def test_wage_and_notes_fields_need_their_permissions(org):
    restricted = EmployeeForm(organization=org, can_view_pay=False, can_view_notes=False)
    assert "default_wage" not in restricted.fields and "notes" not in restricted.fields
    full = EmployeeForm(organization=org, can_view_pay=True, can_view_notes=True)
    assert "default_wage" in full.fields and "notes" in full.fields


def test_wage_falls_back_to_default(org, employee, pay_role):
    employee.default_wage = Decimal("12.00")
    employee.save()
    assert employee.wage_for(pay_role) == Decimal("12.00")
    EmployeePayRole.objects.create(
        organization=org, employee=employee, pay_role=pay_role, wage=Decimal("13.50")
    )
    assert employee.wage_for(pay_role) == Decimal("13.50")


def employee_post_data(**overrides):
    data = {
        "full_name": "Devon Sample",
        "preferred_name": "Devon",
        "email": "Devon@Example.edu",
        "campus_id": "",
        "employment_type": "regular",
        "in_scheduling_pool": "on",
        "tags-TOTAL_FORMS": "0",
        "tags-INITIAL_FORMS": "0",
        "payroles-TOTAL_FORMS": "0",
        "payroles-INITIAL_FORMS": "0",
    }
    data.update(overrides)
    return data


def test_supervisor_adds_employee_with_tag_and_wage(as_user, org, supervisor, pay_role):
    from app.people.models import Tag

    tag = Tag.objects.create(organization=org, name="Cataloguer")
    data = employee_post_data(
        default_wage="12.75",
        **{
            "tags-TOTAL_FORMS": "1",
            "tags-0-tag": str(tag.pk),
            "tags-0-valid_from": "",
            "tags-0-valid_to": "",
            "payroles-TOTAL_FORMS": "1",
            "payroles-0-pay_role": str(pay_role.pk),
            "payroles-0-wage": "13.25",
        },
    )
    response = as_user(supervisor).post(reverse("employee_add"), data)
    assert response.status_code == 302, response.content.decode()[:2000]
    emp = Employee.objects.get(preferred_name="Devon")
    assert emp.organization == org
    assert emp.email == "devon@example.edu"
    assert emp.default_wage == Decimal("12.75")
    assert [t.name for t in emp.tags_on(date(2027, 1, 1))] == ["Cataloguer"]
    assert emp.wage_for(pay_role) == Decimal("13.25")


def test_tag_from_another_organization_is_rejected(as_user, supervisor, other_org):
    from app.people.models import Tag

    foreign = Tag.objects.create(organization=other_org, name="Foreign")
    data = employee_post_data(**{"tags-TOTAL_FORMS": "1", "tags-0-tag": str(foreign.pk)})
    response = as_user(supervisor).post(reverse("employee_add"), data)
    assert response.status_code == 200
    assert not Employee.objects.filter(preferred_name="Devon").exists()


def test_time_bound_tag(org, employee):
    from app.people.models import EmployeeTag, Tag

    lead = Tag.objects.create(organization=org, name="Shift lead")
    EmployeeTag.objects.create(
        organization=org,
        employee=employee,
        tag=lead,
        valid_from=date(2026, 10, 3),
        valid_to=date(2026, 10, 9),
    )
    assert employee.tags_on(date(2026, 10, 5)) == [lead]
    assert employee.tags_on(date(2026, 10, 10)) == []


def test_archive_needs_confirmation_and_never_deletes(as_user, supervisor, employee):
    client = as_user(supervisor)
    url = reverse("employee_archive", args=[employee.pk])
    client.post(url, {})
    employee.refresh_from_db()
    assert employee.archived_at is None
    client.post(url, {"confirm": "yes"})
    employee.refresh_from_db()
    assert employee.archived_at is not None and not employee.in_scheduling_pool
    assert Employee.objects.filter(pk=employee.pk).exists()
    client.post(url, {"action": "restore"})
    employee.refresh_from_db()
    assert employee.archived_at is None


def test_invite_creates_account_and_emails_a_link_only(
    as_user, org, supervisor, employee, django_capture_on_commit_callbacks
):
    employee.default_wage = Decimal("12.50")
    employee.save()
    with django_capture_on_commit_callbacks(execute=True):
        response = as_user(supervisor).post(reverse("employee_invite", args=[employee.pk]))
    assert response.status_code == 302
    employee.refresh_from_db()
    assert employee.user is not None and employee.user.email == "avery@example.edu"
    assert Membership.objects.get(user=employee.user, organization=org).role == Role.EMPLOYEE
    assert len(mail.outbox) == 1
    body = mail.outbox[0].body
    assert "/accounts/password/reset/key/" in body
    assert "12.50" not in body  # D14: no wages or hours in email


def test_invite_without_email_is_refused(as_user, supervisor, employee):
    employee.email = ""
    employee.save()
    as_user(supervisor).post(reverse("employee_invite", args=[employee.pk]))
    employee.refresh_from_db()
    assert employee.user is None
    assert len(mail.outbox) == 0


def test_admin_changes_role_but_not_their_own(as_user, org, admin_user, supervisor):
    client = as_user(admin_user)
    sup_membership = Membership.objects.get(user=supervisor)
    client.post(reverse("members"), {"membership": sup_membership.pk, "role": Role.EMPLOYEE})
    sup_membership.refresh_from_db()
    assert sup_membership.role == Role.EMPLOYEE
    own = Membership.objects.get(user=admin_user)
    client.post(reverse("members"), {"membership": own.pk, "role": Role.EMPLOYEE})
    own.refresh_from_db()
    assert own.role == Role.ADMIN


# --- Audit log ------------------------------------------------------------------------------


def test_wage_change_is_audited_with_who_before_and_after(as_user, org, supervisor, employee):
    employee.default_wage = Decimal("12.00")
    employee.save()
    response = as_user(supervisor).post(
        reverse("employee_edit", args=[employee.pk]),
        employee_post_data(
            full_name=employee.full_name, preferred_name="Avery", email=employee.email, default_wage="12.50"
        ),
    )
    assert response.status_code == 302
    event = (
        Events.objects.filter(pgh_obj_model="people.Employee", pgh_label="update").order_by("-pgh_id").first()
    )
    assert event.pgh_diff["default_wage"] == [12.0, 12.5] or event.pgh_diff["default_wage"] == [
        "12.00",
        "12.50",
    ]
    assert event.pgh_context["user"] == supervisor.pk
    page = as_user(supervisor).get(reverse("audit_log")).content.decode()
    assert "supervisor@example.edu" in page and "default_wage" in page


def test_audit_log_is_scoped_to_the_organization(as_user, supervisor, other_org):
    Employee.objects.create(organization=other_org, full_name="Casey Other", preferred_name="Casey")
    assert "Casey" not in as_user(supervisor).get(reverse("audit_log")).content.decode()


def test_other_supervisors_see_another_org_only_with_membership(as_user, org, other_org):
    sup = make_user(other_org, "other@example.edu", Role.SUPERVISOR)
    Employee.objects.create(organization=org, full_name="Avery Example", preferred_name="Avery")
    assert b"Avery" not in as_user(sup).get(reverse("employees")).content
