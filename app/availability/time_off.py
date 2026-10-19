# SPDX-License-Identifier: AGPL-3.0-or-later
"""Time-off requests (SPEC §9.2): an employee asks, a supervisor sees the coverage impact
first and decides. Approving changes no shifts by itself: shifts that now clash show up as
availability conflicts in the validation suite, with "who could take this shift?" one click
away (CLAUDE.md §2.2–2.3)."""

from __future__ import annotations

from dataclasses import dataclass

import pghistory
from django.db import transaction
from django.utils import timezone

from app.availability.models import TimeOff, TimeOffStatus
from app.people.models import Employee


class TimeOffError(Exception):
    pass


@dataclass(frozen=True)
class Clash:
    shift: object  # app.scheduling.models.Shift
    published: bool


def clashes(t: TimeOff) -> list[Clash]:
    """The employee's live shifts (drafts included) that the time off overlaps."""
    from app.scheduling import services
    from app.scheduling.models import Shift

    rows = (
        Shift.objects.live()
        .filter(employee=t.employee, date__gte=t.start_date, date__lte=t.end_date)
        .select_related("period")
        .order_by("date", "start_min")
    )
    out = []
    states: dict[int, dict[int, str]] = {}
    for s in rows:
        if not s.interval.overlaps(t.interval()):
            continue
        if s.period_id not in states:
            states[s.period_id] = services.shift_states(s.period)
        out.append(Clash(s, states[s.period_id].get(s.pk) == "published"))
    return out


def request(employee: Employee, start_date, end_date, start_min, end_min) -> TimeOff:
    return TimeOff.objects.create(
        organization=employee.organization,
        employee=employee,
        start_date=start_date,
        end_date=end_date,
        start_min=start_min,
        end_min=end_min,
    )


def decide(t: TimeOff, user, approve: bool) -> TimeOff:
    with transaction.atomic(), pghistory.context(action="time_off_decision"):
        t = TimeOff.objects.select_for_update().get(pk=t.pk)
        if t.status != TimeOffStatus.PENDING:
            raise TimeOffError("This request has already been decided or cancelled.")
        t.status = TimeOffStatus.APPROVED if approve else TimeOffStatus.DENIED
        t.decided_by = user
        t.decided_at = timezone.now()
        t.save()
    return t


def cancel(t: TimeOff) -> TimeOff:
    """The employee withdraws a pending or approved request that hasn't started yet."""
    if t.status not in (TimeOffStatus.PENDING, TimeOffStatus.APPROVED):
        raise TimeOffError("Only pending or approved requests can be cancelled.")
    if t.start_date < timezone.localdate():
        raise TimeOffError("Time off that has started can't be cancelled here; ask a supervisor.")
    t.status = TimeOffStatus.CANCELLED
    t.save(update_fields=["status"])
    return t
