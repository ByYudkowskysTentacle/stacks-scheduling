# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sending a notification (D14, D103): always in the app, and by email when the person's
setting for that kind of notification allows. Emails go out only after the transaction
commits, and carry only the person's own news and a link: never wages, hour totals or
co-workers' details."""

from __future__ import annotations

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction

from app.scheduling.models import EMAIL_BY_DEFAULT, EmailPreference, Notification, NotificationCategory


def wants_email(user, organization, category: NotificationCategory) -> bool:
    pref = EmailPreference.objects.filter(user=user, organization=organization, category=category).first()
    return pref.email if pref is not None else category in EMAIL_BY_DEFAULT


def email_settings(user, organization) -> dict[str, bool]:
    return {c.value: wants_email(user, organization, c) for c in NotificationCategory}


def notify(
    user, organization, category: NotificationCategory, subject: str, body: str, link: str = ""
) -> Notification:
    """Create the in-app notification and queue the email if wanted."""
    note = Notification.objects.create(
        organization=organization, user=user, category=category, subject=subject, body=body, link=link
    )
    if user.email and user.is_active and wants_email(user, organization, category):
        address = user.email
        transaction.on_commit(lambda: send_mail(subject, body, None, [address]))
    return note


def site_link(path: str) -> str:
    return settings.STACKS_SITE_URL + path
