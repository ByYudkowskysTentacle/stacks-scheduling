# SPDX-License-Identifier: AGPL-3.0-or-later
"""Availability reminders (SPEC §5.2): sent only when a supervisor turns them on for a term,
or clicks "send now". They go to current employees who haven't entered or confirmed their
availability, at most once a day each, and carry only the term, the deadline and a link."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string
from django.urls import reverse

from app.availability.models import Availability, AvailabilityReminder, Status
from app.core.models import Term
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
    return {
        term.availability_deadline - timedelta(days=term.reminder_days_before),
        term.availability_deadline,
    }


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


def send_due(today: date) -> int:
    terms = Term.objects.filter(availability_reminders=True, availability_deadline__gte=today)
    return sum(send(t, today) for t in terms)
