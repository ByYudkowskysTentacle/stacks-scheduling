# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sending a notification (D14, D103, D125): always in the app, by email when the person's
setting for that kind of notification allows, and by push to their phones (`push.py`).
Emails and pushes go out only after the transaction commits, and carry only the person's own
news and a link: never wages, hour totals or co-workers' details."""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction

from app.scheduling.models import EMAIL_BY_DEFAULT, EmailPreference, Notification, NotificationCategory

log = logging.getLogger(__name__)


def wants_email(user, organization, category: NotificationCategory) -> bool:
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
    if user.email and user.is_active and wants_email(user, organization, category):
        address = user.email
        transaction.on_commit(lambda: _send_email(subject, body, address))
    return note


def _send_email(subject: str, body: str, address: str) -> None:
    """Runs after commit. One failed email must not stop the other emails or anyone's push,
    or turn a committed change into an error page. Logs the error's class only: SMTP errors
    carry the address."""
    try:
        send_mail(subject, body, None, [address])
    except Exception as exc:
        log.warning("Notification email not sent: %s", type(exc).__name__)


def site_link(path: str) -> str:
    return settings.STACKS_SITE_URL + path
