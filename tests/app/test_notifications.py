# SPDX-License-Identifier: AGPL-3.0-or-later
"""Decision notifications and per-person email settings (D103). All data is synthetic."""

from __future__ import annotations

from datetime import date

import pytest
from django.core import mail
from django.urls import reverse

from app.availability import time_off
from app.availability.models import TimeOff, TimeOffStatus
from app.scheduling.models import EmailPreference, Notification, NotificationCategory
from app.scheduling.notify import email_settings, notify

pytestmark = pytest.mark.django_db
MONDAY = date(2027, 1, 18)


def request(employee):
    return TimeOff.objects.create(
        organization=employee.organization, employee=employee, start_date=MONDAY, end_date=MONDAY
    )


def test_time_off_decision_appears_in_the_app_without_email_by_default(
    supervisor, student_user, employee, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        time_off.decide(request(employee), supervisor, approve=True)
    note = Notification.objects.get()
    assert note.user == student_user and note.category == NotificationCategory.TIME_OFF
    assert "was approved" in note.body and note.link == reverse("my_time_off")
    assert mail.outbox == []


def test_time_off_decision_is_emailed_when_turned_on(
    supervisor, student_user, employee, django_capture_on_commit_callbacks
):
    EmailPreference.objects.create(
        organization=employee.organization,
        user=student_user,
        category=NotificationCategory.TIME_OFF,
        email=True,
    )
    with django_capture_on_commit_callbacks(execute=True):
        time_off.decide(request(employee), supervisor, approve=False)
    assert [m.to for m in mail.outbox] == [["avery@example.edu"]]
    assert "not approved" in mail.outbox[0].subject
    assert TimeOff.objects.get().status == TimeOffStatus.DENIED


def test_published_and_reminder_emails_are_on_until_turned_off(
    org, student_user, django_capture_on_commit_callbacks
):
    assert email_settings(student_user, org) == {
        "published": True,
        "time_off": False,
        "exchange": False,
        "reminder": True,
    }
    EmailPreference.objects.create(
        organization=org, user=student_user, category=NotificationCategory.PUBLISHED, email=False
    )
    with django_capture_on_commit_callbacks(execute=True):
        notify(student_user, org, NotificationCategory.PUBLISHED, "Schedule", "Your shifts")
    assert mail.outbox == [] and Notification.objects.count() == 1  # still shown in the app


def test_no_email_to_a_closed_account(org, student_user, django_capture_on_commit_callbacks):
    student_user.is_active = False
    student_user.save()
    with django_capture_on_commit_callbacks(execute=True):
        notify(student_user, org, NotificationCategory.REMINDER, "Reminder", "Please enter availability")
    assert mail.outbox == []


def test_email_settings_page_saves_each_choice(as_user, org, student_user):
    client = as_user(student_user)
    url = reverse("email_settings")
    assert 'name="published" value="on" checked' in client.get(url).content.decode()
    resp = client.post(url, {"time_off": "on", "exchange": "on"}, follow=True)
    assert "Email settings saved." in resp.content.decode()
    assert email_settings(student_user, org) == {
        "published": False,
        "time_off": True,
        "exchange": True,
        "reminder": False,
    }
    assert reverse("email_settings") in client.get(reverse("notifications")).content.decode()


def test_email_settings_are_per_person(as_user, org, student_user, supervisor):
    as_user(student_user).post(reverse("email_settings"), {})
    assert email_settings(supervisor, org)["published"] is True
    assert not EmailPreference.objects.filter(user=supervisor).exists()


def test_email_settings_need_sign_in(client):
    assert client.get(reverse("email_settings")).status_code == 302
