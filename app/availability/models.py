# SPDX-License-Identifier: AGPL-3.0-or-later
"""Term availability (SPEC §5). One `Availability` per employee per term; no row means
`not_entered`, which is never treated as available (CLAUDE.md §7)."""

from __future__ import annotations

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
