# SPDX-License-Identifier: AGPL-3.0-or-later
"""Availability deadline and reminders (SPEC §5.2): off unless a supervisor turns them on,
only to people who haven't entered or confirmed, once a day at most, minimal content."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from django.conf import settings
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


def morning(day: date, hour: int = 10, zone: str = settings.TIME_ZONE) -> datetime:
    """A moment on `day` in the organization's zone, after reminders start for the day (D119)."""
    return datetime.combine(day, time(hour), tzinfo=ZoneInfo(zone))


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
    assert reminders.send_due(morning(DEADLINE)) == 0
    assert not AvailabilityReminder.objects.exists() and mail.outbox == []


@override_settings(STACKS_SITE_URL="https://stacks.example.edu")
def test_reminders_go_out_on_the_reminder_days_only_once(term, django_capture_on_commit_callbacks):
    term.availability_reminders = True
    term.save()
    assert reminders.send_due(morning(DEADLINE - timedelta(days=1))) == 0  # not a reminder day
    with django_capture_on_commit_callbacks(execute=True):
        assert reminders.send_due(morning(DEADLINE - timedelta(days=3))) == 2
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
        assert reminders.send_due(morning(DEADLINE - timedelta(days=3))) == 0  # already sent today
    assert len(mail.outbox) == 2
    with django_capture_on_commit_callbacks(execute=True):
        assert reminders.send_due(morning(DEADLINE)) == 2  # the deadline day itself
    assert reminders.send_due(morning(DEADLINE + timedelta(days=1))) == 0  # past the deadline


def test_supervisor_sends_by_hand_with_confirmation(as_user, supervisor, term):
    client = as_user(supervisor)
    url = reverse("availability_reminders") + f"?term={term.pk}"
    page = client.get(url).content.decode()
    assert "Not entered or not confirmed (3)" in page and "no sign-in account yet" in page
    assert "Scheduled reminders are off" in page
    assert "Send 2 reminders" in page and "the 2 of 3 people above" in page
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
            "reminder_days": "7, 3,1",
        },
    )
    assert resp.status_code == 302
    term.refresh_from_db()
    assert term.availability_reminders and term.reminder_days == [7, 3, 1]
    assert reminders.reminder_days(term) == {date(2027, 1, 1), date(2027, 1, 5), date(2027, 1, 7), DEADLINE}
    page = as_user(supervisor).get(reverse("term_edit", args=[term.pk])).content.decode()
    assert 'value="7, 3, 1"' in page


def test_reminder_days_are_validated(as_user, supervisor, term):
    data = {"name": term.name, "start_date": "2027-01-11", "end_date": "2027-05-07"}
    client = as_user(supervisor)
    for bad, message in (("3, x", "whole numbers"), ("0", "between 1 and 60"), ("1,2,3,4,5,6", "at most 5")):
        resp = client.post(reverse("term_edit", args=[term.pk]), {**data, "reminder_days": bad})
        assert resp.status_code == 200 and message in resp.content.decode(), bad
    term.refresh_from_db()
    assert term.reminder_days == [3]


def test_several_reminder_days_each_send_once(term, django_capture_on_commit_callbacks):
    term.availability_reminders = True
    term.reminder_days = [7, 1]
    term.save()
    sent = {}
    for offset in (8, 7, 3, 1, 0):
        with django_capture_on_commit_callbacks(execute=True):
            sent[offset] = reminders.send_due(morning(DEADLINE - timedelta(days=offset)))
    assert sent == {8: 0, 7: 2, 3: 0, 1: 2, 0: 2}


def test_scheduled_reminders_wait_for_9am_in_the_organizations_zone(term, django_capture_on_commit_callbacks):
    term.availability_reminders = True
    term.save()
    term.organization.time_zone = "Asia/Tokyo"
    term.organization.save()
    reminder_day = DEADLINE - timedelta(days=3)
    with django_capture_on_commit_callbacks(execute=True):
        assert reminders.send_due(morning(reminder_day, hour=8, zone="Asia/Tokyo")) == 0  # too early there
        # 9:30 in Tokyo is still the evening before in the Americas, but it's Tokyo's day that counts.
        assert reminders.send_due(morning(reminder_day, hour=9, zone="Asia/Tokyo")) == 2
    assert set(AvailabilityReminder.objects.values_list("sent_on", flat=True)) == {reminder_day}


def test_students_see_the_deadline(as_user, student_user, term):
    with mock.patch("django.utils.timezone.localdate", return_value=date(2027, 1, 2)):
        home = as_user(student_user).get(reverse("home")).content.decode()
    assert "Due by Friday 8 January" in home


def test_reminder_task_is_scheduled():
    from procrastinate.contrib.django import app as procrastinate_app  # tasks are autodiscovered

    crons = {p.task.name: p.cron for p in procrastinate_app.periodic_registry.periodic_tasks.values()}
    assert crons["availability.send_reminders"] == "5 * * * *"  # hourly; each org waits for 9am local (D119)
    assert crons["core.retention_purge"] == "17 3 * * *"


def test_no_send_button_when_no_one_can_be_emailed(as_user, supervisor, org, term):
    Employee.objects.filter(organization=org).exclude(full_name="No Account").update(
        archived_at=timezone.now()
    )
    page = as_user(supervisor).get(reverse("availability_reminders") + f"?term={term.pk}").content.decode()
    assert "there's no one to email" in page and "Send " not in page.split("No one above")[1]
