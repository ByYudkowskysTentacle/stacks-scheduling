# SPDX-License-Identifier: AGPL-3.0-or-later
"""Decision notifications, per-person email settings (D103) and notification emails sent by the
worker (D127). Nothing reaches a real mail server. All data is synthetic."""

from __future__ import annotations

import json
import smtplib
import socket
import ssl
from datetime import date, timedelta

import pytest
from django.core import mail
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from procrastinate.contrib.django.models import ProcrastinateJob

from app.availability import time_off
from app.availability.models import TimeOff, TimeOffStatus
from app.core.models import Role
from app.scheduling import notify as notify_module
from app.scheduling.models import EmailPreference, Notification, NotificationCategory
from app.scheduling.notify import email_settings, notify
from tests.conftest import make_user

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
    assert "Notification settings saved." in resp.content.decode()
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


# --- Emails sent by the worker (D127) ---


@pytest.fixture
def worker(settings):
    """Queue emails for the worker, as in production, rather than sending them in the request."""
    settings.STACKS_EMAIL_INLINE = False


def email_jobs():
    return list(ProcrastinateJob.objects.filter(task_name="scheduling.send_email").order_by("id"))


def run(jobs):
    """Run queued jobs as the worker would: the task by its name, with the stored arguments."""
    from procrastinate.contrib.django import app

    for job in jobs:
        app.tasks[job.task_name](**job.args)


def published(user, org, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        return notify(
            user, org, NotificationCategory.PUBLISHED, "Stacks: schedule", "Hi Avery,\n\nYour shifts"
        )


def test_a_publish_queues_the_emails_and_the_worker_sends_them(
    worker, org, location, supervisor, employee, student_user, pay_role, django_capture_on_commit_callbacks
):
    from app.scheduling import services
    from app.scheduling.models import SchedulePeriod, Shift

    period = SchedulePeriod.objects.create(organization=org, location=location, start_date=MONDAY, weeks=2)
    Shift.objects.create(
        organization=org,
        location=location,
        period=period,
        employee=employee,
        date=MONDAY,
        start_min=540,
        end_min=720,
        pay_role=pay_role,
    )
    with django_capture_on_commit_callbacks(execute=True):
        services.publish(period, supervisor)
    assert mail.outbox == []  # nothing sent in the request
    (job,) = email_jobs()
    run([job])
    (sent,) = mail.outbox
    note = Notification.objects.get()
    assert sent.to == ["avery@example.edu"] and sent.subject == note.subject and sent.body == note.body
    assert "Mon 18 Jan 9am–12pm" in sent.body


def test_a_job_holds_ids_only(worker, org, student_user, django_capture_on_commit_callbacks):
    note = published(student_user, org, django_capture_on_commit_callbacks)
    (job,) = email_jobs()
    assert job.args == {"notification_id": note.pk, "attempt": 1}
    stored = json.dumps(job.args).lower()
    assert "avery" not in stored and "schedule" not in stored and "shifts" not in stored
    from app.scheduling.tasks import run_solve

    assert job.queue_name == "notifications" and job.priority > run_solve.priority  # ahead of waiting solves


def test_nothing_is_emailed_when_the_transaction_rolls_back(
    org, student_user, django_capture_on_commit_callbacks
):
    with (
        django_capture_on_commit_callbacks(execute=True) as callbacks,
        pytest.raises(RuntimeError),
        transaction.atomic(),
    ):
        notify(student_user, org, NotificationCategory.PUBLISHED, "Schedule", "Your shifts")
        raise RuntimeError("the change failed")
    assert callbacks == [] and mail.outbox == [] and not Notification.objects.exists()


def test_nothing_is_queued_when_the_transaction_rolls_back(
    worker, org, student_user, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True), pytest.raises(RuntimeError), transaction.atomic():
        notify(student_user, org, NotificationCategory.PUBLISHED, "Schedule", "Your shifts")
        raise RuntimeError("the change failed")
    assert email_jobs() == []


@pytest.mark.parametrize("why", ["email off", "no address", "inactive"])
def test_no_job_for_someone_who_gets_no_email(
    worker, org, student_user, why, django_capture_on_commit_callbacks
):
    if why == "email off":
        EmailPreference.objects.create(
            organization=org, user=student_user, category=NotificationCategory.PUBLISHED, email=False
        )
    elif why == "no address":
        student_user.email = ""
    else:
        student_user.is_active = False
    student_user.save()
    published(student_user, org, django_capture_on_commit_callbacks)
    assert email_jobs() == [] and Notification.objects.count() == 1


@pytest.mark.parametrize("change", ["purged", "erased", "deactivated", "address removed", "email turned off"])
def test_a_job_whose_notification_or_person_has_gone_sends_nothing(
    worker, org, student_user, employee, change, caplog, django_capture_on_commit_callbacks
):
    published(student_user, org, django_capture_on_commit_callbacks)
    jobs = email_jobs()
    assert len(jobs) == 1
    if change == "purged":
        Notification.objects.all().delete()  # retention
    elif change == "erased":
        from app.people import records

        employee.archived_at = timezone.now()
        employee.save()
        records.erase_employee(employee)
    elif change == "deactivated":
        student_user.is_active = False
        student_user.save()
    elif change == "address removed":
        student_user.email = ""
        student_user.save()
    else:
        EmailPreference.objects.create(
            organization=org, user=student_user, category=NotificationCategory.PUBLISHED, email=False
        )
    caplog.clear()
    run(jobs)  # no error
    assert mail.outbox == [] and email_jobs() == jobs  # nothing sent, nothing re-queued
    assert caplog.records == []


def test_one_failed_email_does_not_stop_the_next(
    worker, org, student_user, monkeypatch, caplog, django_capture_on_commit_callbacks
):
    blake = make_user(org, "blake@example.edu", Role.EMPLOYEE)
    published(student_user, org, django_capture_on_commit_callbacks)
    published(blake, org, django_capture_on_commit_callbacks)
    real_send_mail = notify_module.send_mail

    def refuse_avery(subject, body, sender, to):
        if to == ["avery@example.edu"]:
            raise smtplib.SMTPRecipientsRefused({to[0]: (550, b"5.1.1 <avery@example.edu>: no such user")})
        return real_send_mail(subject, body, sender, to)

    monkeypatch.setattr(notify_module, "send_mail", refuse_avery)
    jobs = email_jobs()
    assert len(jobs) == 2
    run(jobs)
    assert [m.to for m in mail.outbox] == [["blake@example.edu"]]
    assert email_jobs() == jobs  # an address refused is not tried again
    assert "SMTPRecipientsRefused" in caplog.text and "avery" not in caplog.text


def failing(monkeypatch, exc):
    tries = []

    def send_mail(subject, body, sender, to):
        tries.append(to)
        raise exc

    monkeypatch.setattr(notify_module, "send_mail", send_mail)
    return tries


def test_a_busy_mail_server_is_tried_four_times_1_4_and_16_minutes_apart(
    worker, org, student_user, monkeypatch, caplog, django_capture_on_commit_callbacks
):
    note = published(student_user, org, django_capture_on_commit_callbacks)
    tries = failing(monkeypatch, smtplib.SMTPServerDisconnected("Connection unexpectedly closed"))
    delays = []
    for attempt in (1, 2, 3, 4):
        job = email_jobs()[-1]
        assert job.args == {"notification_id": note.pk, "attempt": attempt}
        started = timezone.now()
        run([job])
        if attempt < 4:
            retry = email_jobs()[-1]
            delays.append(round((retry.scheduled_at - started).total_seconds() / 60))
    assert len(tries) == 4 and delays == [1, 4, 16]
    assert len(email_jobs()) == 4  # the fourth try queued no fifth
    assert "not sent after 4 tries: SMTPServerDisconnected" in caplog.text and "avery" not in caplog.text


def test_inline_retries_happen_at_once(org, student_user, monkeypatch, django_capture_on_commit_callbacks):
    tries = failing(monkeypatch, smtplib.SMTPResponseException(421, b"4.7.0 Try again later"))
    published(student_user, org, django_capture_on_commit_callbacks)
    assert len(tries) == 4  # STACKS_EMAIL_INLINE in tests


@pytest.mark.parametrize(
    ("exc", "retry"),
    [
        (smtplib.SMTPServerDisconnected("Connection unexpectedly closed"), True),
        (smtplib.SMTPConnectError(421, b"Too many connections"), True),
        (smtplib.SMTPSenderRefused(451, b"4.3.0 Try again later", "stacks@example.edu"), True),
        (smtplib.SMTPRecipientsRefused({"avery@example.edu": (450, b"4.2.1 Slow down")}), True),
        (smtplib.SMTPDataError(452, b"4.3.1 Insufficient system storage"), True),
        (smtplib.SMTPAuthenticationError(454, b"4.7.0 Temporary authentication failure"), True),
        (TimeoutError("timed out"), True),
        (ConnectionRefusedError(111, "Connection refused"), True),
        (socket.gaierror(-3, "Temporary failure in name resolution"), True),
        (smtplib.SMTPRecipientsRefused({"avery@example.edu": (550, b"5.1.1 No such user")}), False),
        (smtplib.SMTPSenderRefused(553, b"5.7.1 Sender not allowed", "stacks@example.edu"), False),
        (smtplib.SMTPAuthenticationError(535, b"5.7.8 Bad credentials"), False),
        (smtplib.SMTPDataError(554, b"5.6.0 Message refused"), False),
        (smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server."), False),
        (smtplib.SMTPException("SMTP AUTH extension not supported by server."), False),
        (ssl.SSLCertVerificationError("certificate verify failed"), False),
        (ValueError("Header values can't contain newlines"), False),
    ],
)
def test_only_errors_that_may_clear_are_tried_again(
    worker, org, student_user, monkeypatch, caplog, exc, retry, django_capture_on_commit_callbacks
):
    published(student_user, org, django_capture_on_commit_callbacks)
    failing(monkeypatch, exc)
    run(email_jobs())
    assert len(email_jobs()) == (2 if retry else 1)
    assert type(exc).__name__ in caplog.text and "avery" not in caplog.text


def test_a_mail_server_that_stops_answering_times_out_and_is_tried_again(
    worker, settings, org, student_user, django_capture_on_commit_callbacks
):
    """A real SMTP connection to a server that accepts and then says nothing (SMTP_TIMEOUT)."""
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(1)
    settings.EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    settings.EMAIL_HOST, settings.EMAIL_PORT = silent.getsockname()
    settings.EMAIL_USE_TLS = False
    settings.EMAIL_TIMEOUT = 1
    try:
        published(student_user, org, django_capture_on_commit_callbacks)
        started = timezone.now()
        run(email_jobs())
        took = timezone.now() - started
    finally:
        silent.close()
    assert took < timedelta(seconds=5)  # gave up after SMTP_TIMEOUT, not the 10-second default
    assert email_jobs()[-1].args["attempt"] == 2


def test_emails_give_up_on_the_mail_server_well_before_the_web_request_times_out():
    from django.core.mail import get_connection
    from django.core.mail.backends.smtp import EmailBackend

    connection = get_connection("django.core.mail.backends.smtp.EmailBackend")
    assert isinstance(connection, EmailBackend)
    assert connection.timeout is not None and 0 < connection.timeout <= 15  # gunicorn's limit is 30
