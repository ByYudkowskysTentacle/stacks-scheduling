# SPDX-License-Identifier: AGPL-3.0-or-later
"""Term availability (SPEC §5). One `Availability` per employee per term; no row means
`not_entered`, which is never treated as available (CLAUDE.md §7)."""

from __future__ import annotations

from datetime import date, timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from app.core.audit import audited
from app.core.fields import minute_field
from app.core.models import WEEKDAY_CHOICES, Organization, Term
from app.people.models import Employee
from domain import availability as dom
from domain.timeutil import Interval


class Status(models.TextChoices):
    ENTERED = dom.AvailabilityStatus.ENTERED, "Entered"
    NOT_ENTERED = dom.AvailabilityStatus.NOT_ENTERED, "Not entered"
    NEEDS_REVIEW = dom.AvailabilityStatus.NEEDS_REVIEW, "Needs review"


class Kind(models.TextChoices):
    UNAVAILABLE = dom.BlockKind.UNAVAILABLE, "Unavailable"
    PREFERRED = dom.BlockKind.PREFERRED, "Preferred"


@audited()
class Availability(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="availabilities")
    term = models.ForeignKey(Term, on_delete=models.PROTECT, related_name="availabilities")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ENTERED)
    updated_at = models.DateTimeField(auto_now=True)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name_plural = "availability"
        constraints = [models.UniqueConstraint(fields=["employee", "term"], name="availability_unique")]

    def __str__(self) -> str:
        return f"{self.employee} – {self.term}"

    def to_domain(self) -> dom.WeeklyAvailability:
        return dom.WeeklyAvailability(
            dom.AvailabilityStatus(self.status), tuple(b.to_domain() for b in self.blocks.all())
        )


@audited()
class AvailabilityBlock(models.Model):
    """A weekday time range, kept exactly as entered or imported (SPEC §3.3)."""

    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    availability = models.ForeignKey(Availability, on_delete=models.CASCADE, related_name="blocks")
    weekday = models.PositiveSmallIntegerField(choices=WEEKDAY_CHOICES)
    kind = models.CharField(max_length=20, choices=Kind.choices)
    start_min = minute_field("from")
    end_min = minute_field("until")

    class Meta:
        ordering = ["weekday", "kind", "start_min"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(start_min__lt=models.F("end_min")), name="avail_block_order"
            )
        ]

    def __str__(self) -> str:
        span = Interval(self.start_min, self.end_min).label()
        return f"{self.get_kind_display()} {self.get_weekday_display()} {span}"

    def clean(self) -> None:
        if self.start_min >= self.end_min:
            raise ValidationError("The block ends before it starts.")

    def to_domain(self) -> dom.AvailabilityBlock:
        return dom.AvailabilityBlock(
            self.weekday, dom.BlockKind(self.kind), Interval(self.start_min, self.end_min)
        )


NOT_ENTERED = dom.WeeklyAvailability(dom.AvailabilityStatus.NOT_ENTERED)


def availability_for(employees, term: Term) -> dict[int, dom.WeeklyAvailability]:
    """Domain availability per employee id for a term; missing rows are `not_entered`."""
    rows = Availability.objects.filter(term=term, employee__in=employees).prefetch_related("blocks")
    found = {a.employee_id: a.to_domain() for a in rows}
    return {e.pk: found.get(e.pk, NOT_ENTERED) for e in employees}


class TimeOffStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    DENIED = "denied", "Denied"
    CANCELLED = "cancelled", "Cancelled"


@audited()
class TimeOff(models.Model):
    """A date-specific exception (SPEC §5.1): a request for time off, approved by a supervisor
    (§9.2). Only approved time off blocks scheduling. No reason is collected (SPEC §11)."""

    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="time_off")
    start_date = models.DateField()
    end_date = models.DateField()
    start_min = minute_field("from", null=True, blank=True)
    end_min = minute_field("until", null=True, blank=True)
    status = models.CharField(max_length=10, choices=TimeOffStatus.choices, default=TimeOffStatus.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name_plural = "time off"
        ordering = ["start_date", "start_min"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(start_date__lte=models.F("end_date")), name="timeoff_dates"
            ),
            models.CheckConstraint(
                condition=(models.Q(start_min__isnull=True) & models.Q(end_min__isnull=True))
                | models.Q(start_min__lt=models.F("end_min")),
                name="timeoff_times",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.employee}: {self.label()}"

    @property
    def all_day(self) -> bool:
        return self.start_min is None

    def label(self) -> str:
        days = f"{self.start_date:%a %-d %b}"
        if self.end_date != self.start_date:
            days += f" to {self.end_date:%a %-d %b}"
        if self.all_day:
            return f"{days}, all day"
        return f"{days}, {Interval(self.start_min, self.end_min).label()}"  # type: ignore[arg-type]

    def days(self) -> list[date]:
        return [
            self.start_date + timedelta(days=i) for i in range((self.end_date - self.start_date).days + 1)
        ]

    def interval(self) -> Interval:
        return Interval(0, 1440) if self.all_day else Interval(self.start_min, self.end_min)  # type: ignore[arg-type]


def time_off_for(employees, first: date, last: date) -> dict[int, dict[date, tuple[Interval, ...]]]:
    """Approved time off per employee id and day, within [first, last]."""
    out: dict[int, dict[date, list[Interval]]] = {}
    rows = TimeOff.objects.filter(
        employee__in=employees, status=TimeOffStatus.APPROVED, start_date__lte=last, end_date__gte=first
    )
    for t in rows:
        for d in t.days():
            if first <= d <= last:
                out.setdefault(t.employee_id, {}).setdefault(d, []).append(t.interval())
    return {e: {d: tuple(ivs) for d, ivs in by_day.items()} for e, by_day in out.items()}


class AvailabilityReminder(models.Model):
    """One reminder email sent (SPEC §5.2), so a person gets at most one per day."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    term = models.ForeignKey(Term, on_delete=models.CASCADE, related_name="reminders")
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="availability_reminders")
    sent_on = models.DateField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["term", "employee", "sent_on"], name="availability_reminder_once_a_day"
            )
        ]

    def __str__(self) -> str:
        return f"Reminder to {self.employee} for {self.term} on {self.sent_on}"
