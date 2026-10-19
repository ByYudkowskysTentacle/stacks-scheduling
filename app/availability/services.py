# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from django.db import transaction
from django.utils import timezone

from app.availability.models import Availability, AvailabilityBlock, Status, availability_for
from app.core.models import Location, Term
from domain import calendar as cal
from domain.availability import AvailabilityBlock as DomBlock
from domain.availability import WeekSummary, summarise_week
from domain.timeutil import Interval


@transaction.atomic
def save_availability(employee, term: Term, blocks: Sequence[DomBlock], user) -> Availability:
    """Replace an employee's weekly pattern for a term. Saving it counts as confirming it."""
    availability, _ = Availability.objects.get_or_create(
        employee=employee, term=term, defaults={"organization": employee.organization}
    )
    availability.status = Status.ENTERED
    availability.confirmed_by = user
    availability.confirmed_at = timezone.now()
    availability.save()
    availability.blocks.all().delete()
    AvailabilityBlock.objects.bulk_create(
        AvailabilityBlock(
            organization=employee.organization,
            availability=availability,
            weekday=b.weekday,
            kind=str(b.kind),
            start_min=b.interval.start,
            end_min=b.interval.end,
        )
        for b in blocks
    )
    return availability


def confirm_availability(availability: Availability, user) -> None:
    availability.status = Status.ENTERED
    availability.confirmed_by = user
    availability.confirmed_at = timezone.now()
    availability.save(update_fields=["status", "confirmed_by", "confirmed_at", "updated_at"])


def representative_week(org, term: Term, today: date) -> date:
    """The week to show a weekly pattern against: this week if it's in the term, else the
    term's first full week."""
    start = cal.week_start_for(today, org.week_start)
    if term.contains(today):
        return start
    first = cal.week_start_for(term.start_date, org.week_start)
    return first if first >= term.start_date else first + timedelta(days=7)


@dataclass(frozen=True)
class GridRange:
    """The rows of the entry grid: from the earliest opening to the latest closing in the week."""

    start: int
    end: int
    open_by_weekday: dict[int, list[Interval]]


def grid_range(location: Location | None, week_start: date) -> GridRange:
    if location is None:
        return GridRange(7 * 60, 22 * 60, {wd: [] for wd in range(7)})
    hours, closures = location.domain_hours(), location.domain_closures()
    open_by = {d.weekday(): cal.open_intervals(d, hours, closures) for d in cal.week_days(week_start)}
    ivs = [iv for day in open_by.values() for iv in day]
    if not ivs:
        return GridRange(7 * 60, 22 * 60, open_by)
    start = min(iv.start for iv in ivs) // 60 * 60
    end = -(-max(iv.end for iv in ivs) // 60) * 60
    return GridRange(start, end, open_by)


def week_summaries(employees, term: Term, location: Location, week_start: date) -> dict[int, WeekSummary]:
    hours, closures = location.domain_hours(), location.domain_closures()
    avail = availability_for(employees, term)
    return {pk: summarise_week(a, week_start, hours, closures) for pk, a in avail.items()}
