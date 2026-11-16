# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sending a notification (D14, D103, D125, D127): always in the app, by email when the person's
setting for that kind of notification allows, and by push to their phones (`push.py`).
Emails and pushes go out from the worker after the transaction commits, and carry only the
person's own news and a link: never wages, hour totals or co-workers' details."""

from __future__ import annotations

import logging
import smtplib
import ssl

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction

from app.scheduling.models import EMAIL_BY_DEFAULT, EmailPreference, Notification, NotificationCategory

log = logging.getLogger(__name__)

# Tries for a mail server that is unreachable or says "try again later", the first included:
# 1, 4 and 16 minutes apart (D127). A refusal (5xx) is never tried again.
MAX_EMAIL_ATTEMPTS = 4


def wants_email(user, organization, category: NotificationCategory | str) -> bool:
    pref = EmailPreference.objects.filter(user=user, organization=organization, category=category).first()
    return pref.email if pref is not None else category in EMAIL_BY_DEFAULT


def email_settings(user, organization) -> dict[str, bool]:
    return {c.value: wants_email(user, organization, c) for c in NotificationCategory}


def notify(
    user,
    organization,
    category: NotificationCategory,
    subject: str,
    body: str,
    link: str = "",
    summary: str = "",
) -> Notification:
    """Create the in-app notification and queue the email and push if wanted. `summary` is the
    one line a phone's lock screen shows; without it, the body's first paragraph."""
    from app.scheduling import push

    note = Notification.objects.create(
        organization=organization,
        user=user,
        category=category,
        subject=subject,
        body=body,
        link=link,
        summary=summary[:200],
    )
    push.queue(note)
    queue_email(note)
    return note


# --- Email, sent by the worker (D127) ---


def _can_email(note: Notification) -> bool:
    user = note.user
    return bool(user.email) and user.is_active and wants_email(user, note.organization, note.category)


def queue_email(note: Notification) -> None:
    """Called by `notify()`: email this notification after the transaction commits, if wanted.
    `deliver_email` checks again when it sends: the account or the choice may change first."""
    if not _can_email(note):
        return
    pk = note.pk
    transaction.on_commit(lambda: _dispatch_email_after_commit(pk))


def _dispatch_email_after_commit(notification_id: int) -> None:
    """Runs after commit, next to other people's emails and pushes: a failure here must not
    stop theirs, or turn a committed change into an error page."""
    try:
        dispatch_email(notification_id)
    except Exception as exc:
        log.warning("Email for notification %s not queued: %s", notification_id, type(exc).__name__)


def dispatch_email(notification_id: int, attempt: int = 1, delay: int = 0) -> None:
    if settings.STACKS_EMAIL_INLINE:
        deliver_email(notification_id, attempt)
        return
    from app.scheduling.tasks import send_email

    task = send_email.configure(schedule_in={"seconds": delay}) if delay else send_email
    # Ids only: Procrastinate stores a job's arguments and logs them when the job starts.
    task.defer(notification_id=notification_id, attempt=attempt)


def deliver_email(notification_id: int, attempt: int = 1) -> None:
    """Email one notification, read from the database when the job runs. Nothing is sent if
    it has gone (erased, purged) or the person no longer wants or can get it. Logs name the
    notification by its number and the error by its class only: SMTP errors carry the address."""
    note = Notification.objects.select_related("user", "organization").filter(pk=notification_id).first()
    if note is None or not _can_email(note):
        return
    try:
        send_mail(note.subject, note.body, None, [note.user.email])
        return
    except Exception as exc:
        error = type(exc).__name__
        retry = _worth_retrying(exc)
    # Outside the except block, so nothing raised from here on carries the SMTP error with it.
    if not retry:
        log.warning("Email for notification %s not sent: %s", notification_id, error)
    elif attempt < MAX_EMAIL_ATTEMPTS:
        delay = 60 * 4 ** (attempt - 1)  # 1 minute, then 4, then 16
        log.info(
            "Email for notification %s not sent (%s); trying again in %ss", notification_id, error, delay
        )
        dispatch_email(notification_id, attempt + 1, delay=delay)
    else:
        log.warning("Email for notification %s not sent after %s tries: %s", notification_id, attempt, error)


def _worth_retrying(exc: Exception) -> bool:
    """Whether the same email might go later: the mail server was unreachable, timed out or
    hung up, or answered 4xx ("try again later"). A 5xx answer (address, sender, sign-in or
    message refused), a missing STARTTLS or AUTH, an untrusted certificate or a message Stacks
    built wrongly won't change by waiting."""
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = [code for code, _ in exc.recipients.values()]
        return bool(codes) and all(400 <= code < 500 for code in codes)
    if isinstance(exc, smtplib.SMTPResponseException):
        return 400 <= exc.smtp_code < 500
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return True
    if isinstance(exc, smtplib.SMTPException | ssl.SSLCertVerificationError):
        return False
    return isinstance(exc, OSError)  # refused, reset, timed out, host not found


def site_link(path: str) -> str:
    return settings.STACKS_SITE_URL + path
