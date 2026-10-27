# SPDX-License-Identifier: AGPL-3.0-or-later
"""Availability reminders (SPEC §5.2): sent only when a supervisor turns them on for a term,
or clicks "send now". They go to current employees who haven't entered or confirmed their
availability, at most once a day each, and carry only the term, the deadline and a link."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from app.availability.models import Availability, AvailabilityReminder, Status
from app.core.models import Term
from app.core.timezones import org_time, zone
from app.people.models import Employee


@dataclass(frozen=True)
class Due:
    employee: Employee
    status: str  # not_entered or needs_review

    @property
    def can_remind(self) -> bool:
        user = self.employee.user
        return user is not None and user.is_active


def due_people(term: Term) -> list[Due]:
    """Current employees whose availability for `term` is missing or unconfirmed."""
    statuses = dict(Availability.objects.filter(term=term).values_list("employee_id", "status"))
    people = (
        Employee.objects.filter(organization=term.organization, archived_at__isnull=True)
        .exclude(employment_type="inactive")
        .select_related("user")
        .order_by("preferred_name")
    )
    out = []
    for e in people:
        status = statuses.get(e.pk, Status.NOT_ENTERED)
        if status in (Status.NOT_ENTERED, Status.NEEDS_REVIEW):
            out.append(Due(e, status))
    return out


def reminder_days(term: Term) -> set[date]:
    if term.availability_deadline is None:
        return set()
    before = {term.availability_deadline - timedelta(days=d) for d in term.reminder_days or []}
    return before | {term.availability_deadline}


def send(term: Term, today: date, *, by_hand: bool = False) -> int:
    """Send today's reminders for `term`. Scheduled runs send only when reminders are on and
    today is a reminder day; `by_hand` is a supervisor's explicit "send now"."""
    from app.scheduling.models import NotificationCategory
    from app.scheduling.notify import notify

    if not by_hand and (not term.availability_reminders or today not in reminder_days(term)):
        return 0
    link = reverse("my_availability") + f"?term={term.pk}"
    sent = 0
    for due in due_people(term):
        if not due.can_remind:
            continue
        e = due.employee
        try:
            with transaction.atomic():
                AvailabilityReminder.objects.create(
                    organization=term.organization, term=term, employee=e, sent_on=today
                )
        except IntegrityError:
            continue  # already reminded today
        subject = f"{settings.STACKS_SITE_NAME}: please {'confirm' if due.status == Status.NEEDS_REVIEW else 'enter'} your availability for {term.name}"
        body = render_to_string(
            "availability/email/reminder.txt",
            {
                "employee": e,
                "term": term,
                "needs_review": due.status == Status.NEEDS_REVIEW,
                "link": settings.STACKS_SITE_URL + link,
            },
        )
        notify(e.user, term.organization, NotificationCategory.REMINDER, subject, body, link)
        sent += 1
    return sent


# Scheduled reminders go out from this hour of the organization's day (D119).
SEND_FROM_HOUR = 9


def send_due(now: datetime | None = None) -> int:
    """Send today's scheduled reminders for every organization that has reached
    `SEND_FROM_HOUR` in its own time zone. Runs hourly; `send` sends each person at most once a
    day, so later runs the same day only reach people who became due since."""
    now = now or timezone.now()
    terms = Term.objects.filter(availability_reminders=True).select_related("organization")
    sent = 0
    for term in terms:
        local = now.astimezone(zone(term.organization))
        if local.hour < SEND_FROM_HOUR or term.availability_deadline is None:
            continue
        if term.availability_deadline >= local.date():
            with org_time(term.organization):
                sent += send(term, local.date())
    return sent
