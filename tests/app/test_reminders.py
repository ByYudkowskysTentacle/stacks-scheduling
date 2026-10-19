# SPDX-License-Identifier: AGPL-3.0-or-later
"""Availability deadline and reminders (SPEC §5.2): off unless a supervisor turns them on,
only to people who haven't entered or confirmed, once a day at most, minimal content."""

from __future__ import annotations

from datetime import date, timedelta
from unittest import mock

import pytest
from django.core import mail
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from app.availability import reminders
from app.availability.models import Availability, AvailabilityReminder, Status
from app.core.models import Role, Term
from app.people.models import Employee
from app.scheduling.models import Notification
from tests.conftest import make_user

pytestmark = pytest.mark.django_db
DEADLINE = date(2027, 1, 8)


@pytest.fixture
def term(org, employee, student_user):
    t = Term.objects.create(
        organization=org,
        name="Spring 2027",
        start_date=date(2027, 1, 11),
        end_date=date(2027, 5, 7),
        availability_deadline=DEADLINE,
    )
    # Someone who has entered theirs, someone imported (needs review), someone with no account,
    # and someone archived.
    done = Employee.objects.create(organization=org, full_name="Done Person", preferred_name="Done")
    done.user = make_user(org, "done@example.edu", Role.EMPLOYEE)
    done.save()
    Availability.objects.create(organization=org, employee=done, term=t, status=Status.ENTERED)
    imported = Employee.objects.create(
        organization=org, full_name="Imported Person", preferred_name="Imported"
    )
    imported.user = make_user(org, "imported@example.edu", Role.EMPLOYEE)
    imported.save()
    Availability.objects.create(organization=org, employee=imported, term=t, status=Status.NEEDS_REVIEW)
    Employee.objects.create(organization=org, full_name="No Account", preferred_name="Noacct")
    gone = Employee.objects.create(
        organization=org, full_name="Gone Person", preferred_name="Gone", archived_at=timezone.now()
    )
    gone.user = make_user(org, "gone@example.edu", Role.EMPLOYEE)
    gone.save()
    return t


def test_due_people_are_those_not_entered_or_not_confirmed(term):
    due = {d.employee.preferred_name: (d.status, d.can_remind) for d in reminders.due_people(term)}
    assert due == {
        "Avery": (Status.NOT_ENTERED, True),
        "Imported": (Status.NEEDS_REVIEW, True),
        "Noacct": (Status.NOT_ENTERED, False),
    }


def test_nothing_is_sent_while_reminders_are_off(term):
    assert reminders.send_due(DEADLINE) == 0
    assert not AvailabilityReminder.objects.exists() and mail.outbox == []


@override_settings(STACKS_SITE_URL="https://stacks.example.edu")
def test_reminders_go_out_on_the_reminder_days_only_once(term, django_capture_on_commit_callbacks):
    term.availability_reminders = True
    term.save()
    assert reminders.send_due(DEADLINE - timedelta(days=1)) == 0  # not a reminder day
    with django_capture_on_commit_callbacks(execute=True):
        assert reminders.send_due(DEADLINE - timedelta(days=3)) == 2
    assert sorted(m.to[0] for m in mail.outbox) == ["avery@example.edu", "imported@example.edu"]
    avery = next(m for m in mail.outbox if m.to == ["avery@example.edu"])
    assert "please enter your availability for Spring 2027" in avery.subject
    assert "by Friday 8 January" in avery.body
    assert f"https://stacks.example.edu{reverse('my_availability')}?term={term.pk}" in avery.body
    for other in ("Imported", "Done", "Noacct", "Gone"):
        assert other not in avery.body
    imported = next(m for m in mail.outbox if m.to == ["imported@example.edu"])
    assert "confirm" in imported.subject
    assert Notification.objects.count() == 2
    with django_capture_on_commit_callbacks(execute=True):
        assert reminders.send_due(DEADLINE - timedelta(days=3)) == 0  # already sent today
    assert len(mail.outbox) == 2
    with django_capture_on_commit_callbacks(execute=True):
        assert reminders.send_due(DEADLINE) == 2  # the deadline day itself
    assert reminders.send_due(DEADLINE + timedelta(days=1)) == 0  # past the deadline


def test_supervisor_sends_by_hand_with_confirmation(as_user, supervisor, term):
    client = as_user(supervisor)
    url = reverse("availability_reminders") + f"?term={term.pk}"
    page = client.get(url).content.decode()
    assert "Not entered or not confirmed (3)" in page and "no sign-in account yet" in page
    assert "Scheduled reminders are off" in page
    resp = client.post(reverse("availability_reminders"), {"term": term.pk}, follow=True)
    assert "Tick the confirmation box" in resp.content.decode()
    resp = client.post(reverse("availability_reminders"), {"term": term.pk, "confirm": "yes"}, follow=True)
    assert "Sent 2 reminders" in resp.content.decode()


def test_students_and_leads_cannot_send_reminders(as_user, student_user, lead_user, term):
    for user in (student_user, lead_user):
        assert as_user(user).get(reverse("availability_reminders")).status_code == 403


def test_supervisor_turns_reminders_on_in_term_settings(as_user, supervisor, term):
    resp = as_user(supervisor).post(
        reverse("term_edit", args=[term.pk]),
        {
            "name": term.name,
            "start_date": "2027-01-11",
            "end_date": "2027-05-07",
            "availability_deadline": "2027-01-08",
            "availability_reminders": "on",
            "reminder_days_before": "5",
        },
    )
    assert resp.status_code == 302
    term.refresh_from_db()
    assert term.availability_reminders and term.reminder_days_before == 5
    assert reminders.reminder_days(term) == {date(2027, 1, 3), DEADLINE}


def test_students_see_the_deadline(as_user, student_user, term):
    with mock.patch("django.utils.timezone.localdate", return_value=date(2027, 1, 2)):
        home = as_user(student_user).get(reverse("home")).content.decode()
    assert "Due by Friday 8 January" in home


def test_reminder_task_is_scheduled():
    from procrastinate.contrib.django import app as procrastinate_app  # tasks are autodiscovered

    crons = {p.task.name: p.cron for p in procrastinate_app.periodic_registry.periodic_tasks.values()}
    assert crons["availability.send_reminders"] == "5 14 * * *"
    assert crons["core.retention_purge"] == "17 3 * * *"
