# SPDX-License-Identifier: AGPL-3.0-or-later
"""Milestone 3: draft shifts, validation after every change, publishing and shift-lead rights.
Covers acceptance scenarios 15, 16 and 17 (SPEC §12)."""

from __future__ import annotations

from datetime import date

import pytest
from django.core import mail
from django.urls import reverse

from app.core.models import Department, Location, Membership, Role, Term
from app.people.models import Employee, PayRole, Tag
from app.scheduling import services
from app.scheduling.models import (
    CallOut,
    HourCapRule,
    Notification,
    PublishedVersion,
    SchedulePeriod,
    SchedulingRules,
    Shift,
    TagCoverageRule,
)
from tests.conftest import make_user

pytestmark = pytest.mark.django_db
START = date(2027, 1, 16)  # Saturday
MONDAY = date(2027, 1, 18)


@pytest.fixture
def term(org):
    return Term.objects.create(
        organization=org, name="Spring 2027", start_date=date(2027, 1, 11), end_date=date(2027, 5, 7)
    )


@pytest.fixture
def period(org, location, term):
    return SchedulePeriod.objects.create(organization=org, location=location, start_date=START, weeks=2)


@pytest.fixture
def fws(org):
    return PayRole.objects.create(organization=org, name="FWS Student Assistant", color="#7a3e9d")


def add_shift(period, employee, day=MONDAY, start=540, end=720, **kw):
    return Shift.objects.create(
        organization=period.organization,
        location=period.location,
        period=period,
        employee=employee,
        date=day,
        start_min=start,
        end_min=end,
        pay_role=employee.default_pay_role if employee else None,
        **kw,
    )


def shift_post(employee=None, day=MONDAY, start="09:00", end="12:00", pay_role=""):
    return {
        "employee": employee.pk if employee else "",
        "date": day.isoformat(),
        "start_min": start,
        "end_min": end,
        "pay_role": pay_role,
        "note": "",
    }


# --- Drafts and validation ------------------------------------------------------------------


def test_creating_a_period_snaps_to_the_week_start(as_user, org, supervisor, location):
    dept = Department.objects.create(organization=org, name="Circulation")
    page = as_user(supervisor).get(reverse("schedule_index")).content.decode()
    assert f'value="{dept.pk}" selected' in page  # the only department is preselected
    as_user(supervisor).post(
        reverse("schedule_index"), {"department": dept.pk, "start_date": "2027-01-20", "weeks": 2}
    )
    period = SchedulePeriod.objects.get()
    assert period.start_date == START and period.department == dept


def test_each_department_plans_its_own_period_for_the_same_week(as_user, org, supervisor, location):
    circ = Department.objects.create(organization=org, name="Circulation")
    special = Department.objects.create(organization=org, name="Special Collections")
    client = as_user(supervisor)
    for dept in (circ, special, circ):
        client.post(
            reverse("schedule_index"), {"department": dept.pk, "start_date": "2027-01-18", "weeks": 2}
        )
    assert sorted(p.department.name for p in SchedulePeriod.objects.all()) == [
        "Circulation",
        "Special Collections",
    ]


def test_saving_a_shift_runs_validation_and_says_so(as_user, supervisor, period, employee):
    client = as_user(supervisor)
    response = client.post(reverse("shift_add", args=[period.pk]), shift_post(employee), follow=True)
    shift = Shift.objects.get()
    assert shift.employee == employee and shift.pay_role == employee.default_pay_role
    text = response.content.decode()
    assert "Validation:" in text and "coverage gap" in text  # one person can't cover two-a-slot all period


def test_validation_reports_zero_issues_when_clean(as_user, org, supervisor, period, employee, term):
    from app.availability.models import Availability

    Availability.objects.create(organization=org, employee=employee, term=term)  # entered, free any time
    rules = SchedulingRules.for_org(org)
    rules.coverage_default = 0
    rules.coverage_mode = "minimum"  # in exact mode, anyone on duty over a target of 0 is over-coverage
    rules.save()
    response = as_user(supervisor).post(
        reverse("shift_add", args=[period.pk]), shift_post(employee), follow=True
    )
    assert "Validation: 0 issues" in response.content.decode()


def test_shift_outside_the_period_is_rejected(as_user, supervisor, period, employee):
    response = as_user(supervisor).post(
        reverse("shift_add", args=[period.pk]), shift_post(employee, day=date(2027, 3, 1))
    )
    assert response.status_code == 200 and not Shift.objects.exists()


def test_shift_times_must_be_on_15_minute_boundaries(as_user, supervisor, period, employee):
    as_user(supervisor).post(reverse("shift_add", args=[period.pk]), shift_post(employee, start="09:10"))
    assert not Shift.objects.exists()


def test_removing_a_shift_needs_confirmation_and_is_restorable(as_user, supervisor, period, employee):
    shift = add_shift(period, employee)
    client = as_user(supervisor)
    client.post(reverse("shift_delete", args=[period.pk, shift.pk]), {})
    shift.refresh_from_db()
    assert shift.deleted_at is None
    client.post(reverse("shift_delete", args=[period.pk, shift.pk]), {"confirm": "yes"})
    shift.refresh_from_db()
    assert shift.deleted_at is not None and Shift.objects.filter(pk=shift.pk).exists()
    client.post(reverse("shift_delete", args=[period.pk, shift.pk]), {"action": "restore"})
    shift.refresh_from_db()
    assert shift.deleted_at is None


def test_period_grid_and_day_view_render(as_user, supervisor, period, employee):
    add_shift(period, employee)
    client = as_user(supervisor)
    page = client.get(reverse("period", args=[period.pk])).content.decode()
    assert "Avery" in page and "9am–12pm" in page and "Validation:" in page
    day = client.get(reverse("period_day", args=[period.pk, "2027-01-20"])).content.decode()
    assert "Closed (Chapel)" in day


# --- Scenario 15: pay role on reassignment --------------------------------------------------


def test_reassigning_moves_the_shift_to_the_new_default_pay_role(
    as_user, org, supervisor, period, employee, pay_role, fws
):
    other = Employee.objects.create(
        organization=org, full_name="Blake Sample", preferred_name="Blake", default_pay_role=fws
    )
    shift = add_shift(period, employee)
    assert shift.pay_role == pay_role
    as_user(supervisor).post(
        reverse("shift_edit", args=[period.pk, shift.pk]), shift_post(other, pay_role=str(pay_role.pk))
    )
    shift.refresh_from_db()
    assert shift.employee == other and shift.pay_role == fws
    page = as_user(supervisor).get(reverse("period", args=[period.pk])).content.decode()
    assert "--chip: #7a3e9d" in page  # the shift's colour follows its pay role


def test_a_deliberately_chosen_pay_role_is_kept(as_user, org, supervisor, period, employee, pay_role, fws):
    shift = add_shift(period, employee)
    as_user(supervisor).post(
        reverse("shift_edit", args=[period.pk, shift.pk]), shift_post(employee, pay_role=str(fws.pk))
    )
    shift.refresh_from_db()
    assert shift.pay_role == fws


# --- Scenario 16: publish gate --------------------------------------------------------------


@pytest.fixture
def invited(org, employee, student_user):
    other_user = make_user(org, "blake@example.edu", Role.EMPLOYEE)
    blake = Employee.objects.create(
        organization=org, full_name="Blake Sample", preferred_name="Blake", user=other_user
    )
    return employee, blake


def test_no_notification_for_draft_changes_and_one_summary_each_on_publish(
    as_user, supervisor, period, invited, django_capture_on_commit_callbacks
):
    avery, blake = invited
    client = as_user(supervisor)
    with django_capture_on_commit_callbacks(execute=True):
        client.post(reverse("shift_add", args=[period.pk]), shift_post(avery))
        client.post(reverse("shift_add", args=[period.pk]), shift_post(avery, start="13:00", end="16:00"))
        client.post(reverse("shift_add", args=[period.pk]), shift_post(blake, day=date(2027, 1, 19)))
    assert mail.outbox == [] and not Notification.objects.exists()

    with django_capture_on_commit_callbacks(execute=True):
        client.post(reverse("publish", args=[period.pk]), {"confirm": "yes"})
    assert PublishedVersion.objects.get().number == 1
    assert sorted(m.to[0] for m in mail.outbox) == ["avery@example.edu", "blake@example.edu"]
    avery_mail = next(m for m in mail.outbox if m.to == ["avery@example.edu"])
    assert "Mon 18 Jan 9am–12pm" in avery_mail.body and "Mon 18 Jan 1pm–4pm" in avery_mail.body
    assert "Blake" not in avery_mail.body  # never co-workers' details
    assert Notification.objects.count() == 2


def test_republishing_notifies_only_people_whose_shifts_changed(
    as_user, supervisor, period, invited, django_capture_on_commit_callbacks
):
    avery, blake = invited
    a = add_shift(period, avery)
    add_shift(period, blake, day=date(2027, 1, 19))
    services.publish(period, supervisor)
    mail.outbox.clear()
    a.start_min = 600
    a.save()
    with django_capture_on_commit_callbacks(execute=True):
        as_user(supervisor).post(reverse("publish", args=[period.pk]), {"confirm": "yes"})
    assert [m.to for m in mail.outbox] == [["avery@example.edu"]]
    body = mail.outbox[0].body
    assert "Removed:" in body and "9am–12pm" in body and "Added:" in body and "10am–12pm" in body


def test_publish_needs_confirmation_and_changes(as_user, supervisor, period, employee):
    client = as_user(supervisor)
    add_shift(period, employee)
    client.post(reverse("publish", args=[period.pk]), {})
    assert not PublishedVersion.objects.exists()
    client.post(reverse("publish", args=[period.pk]), {"confirm": "yes"})
    client.post(reverse("publish", args=[period.pk]), {"confirm": "yes"})  # nothing new
    assert PublishedVersion.objects.count() == 1


def test_students_only_see_published_shifts(as_user, supervisor, student_user, period, employee):
    shift = add_shift(period, employee, day=date(2027, 1, 18))
    client = as_user(student_user)
    from unittest import mock

    with mock.patch("django.utils.timezone.localdate", return_value=date(2027, 1, 16)):
        assert "9am–12pm" not in client.get(reverse("my_schedule")).content.decode()
        services.publish(period, supervisor)
        shift.start_min = 600
        shift.save()  # an unpublished draft change
        page = client.get(reverse("my_schedule")).content.decode()
    assert "9am–12pm" in page and "10am–12pm" not in page


def test_discard_drafts_returns_to_the_published_version(as_user, supervisor, period, employee):
    kept = add_shift(period, employee)
    services.publish(period, supervisor)
    kept.start_min = 600
    kept.save()
    extra = add_shift(period, employee, day=date(2027, 1, 19))
    client = as_user(supervisor)
    client.post(reverse("discard_drafts", args=[period.pk]), {"confirm": "yes"})
    kept.refresh_from_db()
    extra.refresh_from_db()
    assert kept.start_min == 540 and extra.deleted_at is not None
    assert services.pending_changes(period) == {}


# --- Scenario 17: shift lead drafts ---------------------------------------------------------


def test_shift_lead_edits_are_drafts_in_the_review_queue(
    as_user, org, supervisor, lead_user, period, employee, django_capture_on_commit_callbacks
):
    shift = add_shift(period, employee)
    services.publish(period, supervisor)
    mail.outbox.clear()
    lead = as_user(lead_user)
    with django_capture_on_commit_callbacks(execute=True):
        lead.post(reverse("shift_edit", args=[period.pk, shift.pk]), shift_post(employee, start="10:00"))
    shift.refresh_from_db()
    assert shift.start_min == 600 and shift.proposed_by_lead
    assert mail.outbox == [] and not Notification.objects.exists()
    assert lead.post(reverse("publish", args=[period.pk]), {"confirm": "yes"}).status_code == 403
    assert lead.get(reverse("review_queue")).status_code == 403

    queue = as_user(supervisor).get(reverse("review_queue")).content.decode()
    assert "Avery" in queue
    as_user(supervisor).post(reverse("review_queue"), {"action": "revert", "shift": shift.pk})
    shift.refresh_from_db()
    assert shift.start_min == 540 and not shift.proposed_by_lead


def test_shift_lead_records_a_call_out_without_notifying(
    as_user, org, supervisor, lead_user, period, employee, django_capture_on_commit_callbacks
):
    cover = Employee.objects.create(organization=org, full_name="Casey Placeholder", preferred_name="Casey")
    shift = add_shift(period, employee)
    with django_capture_on_commit_callbacks(execute=True):
        as_user(lead_user).post(
            reverse("call_out", args=[period.pk, shift.pk]), {"covered_by": cover.pk, "note": ""}
        )
    shift.refresh_from_db()
    assert shift.employee == cover and shift.proposed_by_lead
    call_out = CallOut.objects.get()
    assert call_out.called_out == employee and call_out.reviewed_at is None
    assert mail.outbox == []
    as_user(supervisor).post(reverse("review_queue"), {"action": "reviewed", "call_out": call_out.pk})
    call_out.refresh_from_db()
    assert call_out.reviewed_by == supervisor


def test_shift_lead_cannot_see_wages_or_remove_shifts(as_user, lead_user, period, employee):
    from decimal import Decimal

    employee.default_wage = Decimal("12.34")
    employee.notes = "Supervisor-only note"
    employee.save()
    shift = add_shift(period, employee)
    lead = as_user(lead_user)
    page = lead.get(reverse("period", args=[period.pk])).content.decode()
    assert "12.34" not in page and "Supervisor-only" not in page
    assert (
        lead.post(reverse("shift_delete", args=[period.pk, shift.pk]), {"confirm": "yes"}).status_code == 403
    )
    assert lead.get(reverse("rules")).status_code == 403


def test_students_cannot_open_the_working_schedule(as_user, student_user, period):
    client = as_user(student_user)
    assert client.get(reverse("period", args=[period.pk])).status_code == 403
    assert client.get(reverse("shift_add", args=[period.pk])).status_code == 403
    assert client.get(reverse("schedule_index")).status_code == 403


def test_other_organizations_periods_are_hidden(as_user, supervisor, other_org):
    from app.core.models import Location

    loc = Location.objects.create(organization=other_org, name="X")
    foreign = SchedulePeriod.objects.create(organization=other_org, location=loc, start_date=START)
    assert as_user(supervisor).get(reverse("period", args=[foreign.pk])).status_code == 404


# --- Rules ----------------------------------------------------------------------------------


def test_rule_change_revalidates_without_changing_shifts(as_user, org, supervisor, period, employee):
    from unittest import mock

    shift = add_shift(period, employee)
    tag = Tag.objects.create(organization=org, name="New employee")
    client = as_user(supervisor)
    with mock.patch("django.utils.timezone.localdate", return_value=date(2027, 1, 16)):
        response = client.post(
            reverse("rules"),
            {"form": "cap", "scope": "period", "minutes": "12", "tag": tag.pk, "employee": "", "label": ""},
            follow=True,
        )
    assert HourCapRule.objects.get().minutes == 720
    assert "Under the new rules" in response.content.decode()
    shift.refresh_from_db()
    assert shift.start_min == 540


def test_new_employee_rule_strictness_is_a_setting(as_user, org, supervisor):
    tag = Tag.objects.create(organization=org, name="New employee")
    client = as_user(supervisor)
    page = client.get(reverse("rules")).content.decode()
    assert "Always with someone who doesn&#x27;t have the tag" in page
    assert "Never working alone (anyone else on duty counts)" in page
    client.post(
        reverse("rules"),
        {"form": "tag_rule", "tag": tag.pk, "kind": "not_solo", "count": 1, "minutes": "", "label": ""},
    )
    rule = TagCoverageRule.objects.get()
    assert rule.kind == "not_solo" and str(rule) == "A New employee never works alone"


def test_rules_form_uses_hours(as_user, org, supervisor):
    data = {
        "form": "rules",
        "coverage_default": 2,
        "coverage_mode": "exact",
        "min_shift_minutes": "2",
        "max_shift_minutes": "4",
        "max_daily_minutes": "6",
        "max_shifts_per_day": 2,
        "min_gap_minutes": 30,
        "default_weekly_cap_minutes": "19.5",
        "freeze_window_hours": 48,
        "change_limit_percent": 30,
        "rotation_mode": "none",
    }
    as_user(supervisor).post(reverse("rules"), data)
    assert SchedulingRules.for_org(org).default_weekly_cap_minutes == 1170


def test_membership_role_change_doesnt_leak_drafts(as_user, org, period):
    # A demoted supervisor loses draft access on the next request.
    user = make_user(org, "former@example.edu", Role.SUPERVISOR)
    client = as_user(user)
    assert client.get(reverse("period", args=[period.pk])).status_code == 200
    Membership.objects.filter(user=user).update(role=Role.EMPLOYEE)
    assert client.get(reverse("period", args=[period.pk])).status_code == 403


def test_shifts_on_another_schedule_count_for_double_booking(org, location, period, employee):
    branch = Location.objects.create(organization=org, name="Branch")
    other = SchedulePeriod.objects.create(organization=org, location=branch, start_date=START, weeks=2)
    add_shift(other, employee, start=600, end=780)
    mine = add_shift(period, employee)
    issues = [i for i in services.validate_period(period).issues if i.category == "double_booking"]
    assert len(issues) == 1 and issues[0].shift_id == mine.pk
    assert "on another schedule" in issues[0].message
