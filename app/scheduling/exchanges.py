# SPDX-License-Identifier: AGPL-3.0-or-later
"""Claiming open shifts and offering shifts for cover (SPEC §9.2).

Employees only ever act on the published schedule. A request names a shift as it was
published; approval re-checks that the working copy still matches, runs the validation suite
on the would-be result, and reassigns the shift as a draft. Nothing is sent to anyone: the
change reaches the published schedule, and notifications, when a supervisor publishes
(CLAUDE.md §2.2).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

import pghistory
from django.db import transaction
from django.utils import timezone

from app.people.models import Employee
from app.scheduling import services
from app.scheduling.models import (
    ACTIVE_EXCHANGE,
    ExchangeKind,
    ExchangeStatus,
    Shift,
    ShiftExchange,
)
from domain import suggest


class ExchangeError(Exception):
    pass


def _published_row(shift: Shift) -> dict | None:
    latest = shift.period.latest_version()
    if latest is None:
        return None
    return next((r for r in latest.shifts if r["id"] == shift.pk), None)


def _matches_published(shift: Shift, employee_id: int | None) -> bool:
    """The shift is published with `employee_id`, and the working copy hasn't moved it since."""
    row = _published_row(shift)
    return (
        row is not None
        and shift.deleted_at is None
        and row["employee_id"] == employee_id
        and shift.employee_id == employee_id
        and (row["date"], row["start"], row["end"])
        == (shift.date.isoformat(), shift.start_min, shift.end_min)
    )


def _future(shift: Shift, today: date) -> None:
    if shift.date < today:
        raise ExchangeError("That shift has already happened.")


def _overlaps_own(employee: Employee, shift: Shift) -> bool:
    return any(
        s.interval.overlaps(shift.interval)
        for s in Shift.objects.live().filter(employee=employee, date=shift.date).exclude(pk=shift.pk)
    )


def claim(employee: Employee, shift: Shift, today: date) -> ShiftExchange:
    _future(shift, today)
    if not _matches_published(shift, None):
        raise ExchangeError("That shift isn't an open shift on the published schedule any more.")
    if _overlaps_own(employee, shift):
        raise ExchangeError("You're already working at that time.")
    if ShiftExchange.objects.filter(shift=shift, taker=employee, status__in=ACTIVE_EXCHANGE).exists():
        raise ExchangeError("You've already asked for that shift.")
    return ShiftExchange.objects.create(
        organization=employee.organization,
        shift=shift,
        kind=ExchangeKind.CLAIM,
        status=ExchangeStatus.PENDING,
        requester=employee,
        taker=employee,
    )


def offer(employee: Employee, shift: Shift, today: date) -> ShiftExchange:
    _future(shift, today)
    if not _matches_published(shift, employee.pk):
        raise ExchangeError("You can only offer your own shifts from the published schedule.")
    if ShiftExchange.objects.filter(shift=shift, status__in=ACTIVE_EXCHANGE).exists():
        raise ExchangeError("That shift is already offered.")
    return ShiftExchange.objects.create(
        organization=employee.organization,
        shift=shift,
        kind=ExchangeKind.COVER,
        status=ExchangeStatus.OPEN,
        requester=employee,
    )


def volunteer(employee: Employee, ex: ShiftExchange, today: date) -> ShiftExchange:
    with transaction.atomic():
        ex = ShiftExchange.objects.select_for_update().select_related("shift").get(pk=ex.pk)
        _future(ex.shift, today)
        if ex.kind != ExchangeKind.COVER or ex.status != ExchangeStatus.OPEN:
            raise ExchangeError("Someone has already volunteered, or the offer was withdrawn.")
        if ex.requester_id == employee.pk:
            raise ExchangeError("That's your own shift.")
        if _overlaps_own(employee, ex.shift):
            raise ExchangeError("You're already working at that time.")
        ex.taker = employee
        ex.status = ExchangeStatus.PENDING
        ex.save()
    return ex


def withdraw(employee: Employee, ex: ShiftExchange) -> ShiftExchange:
    """The requester withdraws the whole request; a volunteer withdraws only themselves,
    which puts the offer back up."""
    with transaction.atomic():
        ex = ShiftExchange.objects.select_for_update().get(pk=ex.pk)
        if ex.status not in ACTIVE_EXCHANGE:
            raise ExchangeError("That request has already been decided.")
        if ex.kind == ExchangeKind.COVER and ex.taker_id == employee.pk and ex.requester_id != employee.pk:
            ex.taker = None
            ex.status = ExchangeStatus.OPEN
        elif ex.requester_id == employee.pk:
            ex.status = ExchangeStatus.WITHDRAWN
        else:
            raise ExchangeError("That isn't your request.")
        ex.save()
    return ex


@dataclass(frozen=True)
class Review:
    exchange: ShiftExchange
    still_matches: bool
    check: suggest.ChangeCheck | None


def review(ex: ShiftExchange) -> Review:
    """What approving would do: the validation suite on the would-be result (SPEC §9.2)."""
    shift = ex.shift
    expected = None if ex.kind == ExchangeKind.CLAIM else ex.requester_id
    if shift.deleted_at is not None or shift.employee_id != expected or ex.taker_id is None:
        return Review(ex, False, None)
    ctx = services.build_context(shift.period)
    target = next(s for s in ctx.shifts if s.id == shift.pk)
    after = [replace(s, employee_id=ex.taker_id) if s is target else s for s in ctx.shifts]
    return Review(ex, True, suggest.check_change(ctx, after))


def decide(ex: ShiftExchange, user, approve: bool) -> ShiftExchange:
    with transaction.atomic(), pghistory.context(action="shift_exchange_decision", exchange=ex.pk):
        ex = (
            ShiftExchange.objects.select_for_update(of=("self",))
            .select_related("shift__period", "taker")
            .get(pk=ex.pk)
        )
        if ex.status != ExchangeStatus.PENDING:
            raise ExchangeError("That request isn't waiting for a decision.")
        if approve:
            shift = Shift.objects.select_for_update().get(pk=ex.shift_id)
            ex.shift = shift
            result = review(ex)
            if not result.still_matches:
                raise ExchangeError(
                    "The shift has changed since the request was made. Deny it, or edit the shift."
                )
            if result.check is not None and not result.check.ok:
                raise ExchangeError("Approving would break a rule: " + "; ".join(result.check.errors))
            taker = ex.taker
            if taker is None:
                raise ExchangeError("Nobody has volunteered for that shift yet.")
            shift.employee = taker
            shift.pay_role = taker.default_pay_role or shift.pay_role
            shift.updated_by = user
            shift.save()
            ShiftExchange.objects.filter(shift=shift, status__in=ACTIVE_EXCHANGE).exclude(pk=ex.pk).update(
                status=ExchangeStatus.DENIED, decided_by=user, decided_at=timezone.now()
            )
        ex.status = ExchangeStatus.APPROVED if approve else ExchangeStatus.DENIED
        ex.decided_by = user
        ex.decided_at = timezone.now()
        ex.save()
        _tell_people(ex)
    return ex


def _tell_people(ex: ShiftExchange) -> None:
    """Tell the people who asked: in the app, and by email only if they turned it on (D103).
    No co-workers' names: only the shift and the outcome."""
    from django.conf import settings
    from django.urls import reverse

    from app.scheduling.models import NotificationCategory
    from app.scheduling.notify import notify, site_link

    shift = ex.shift
    when = f"{shift.date:%a %-d %b} {shift.interval.label()}"
    approved = ex.status == ExchangeStatus.APPROVED
    link = reverse("shift_exchange")
    lines = []
    if ex.kind == ExchangeKind.CLAIM:
        lines.append(
            (
                ex.taker,
                f"Your request for the open shift {when} was {'approved' if approved else 'not approved'}.",
            )
        )
    else:
        if ex.taker is not None:
            lines.append(
                (ex.taker, f"Your offer to cover {when} was {'approved' if approved else 'not approved'}.")
            )
        lines.append(
            (
                ex.requester,
                f"Cover for your shift {when} was {'approved: someone else will work it' if approved else 'not approved: the shift is still yours'}.",
            )
        )
    for employee, text in lines:
        if employee is None or employee.user is None:
            continue
        tail = " It shows on the schedule once a supervisor publishes it." if approved else ""
        body = f"Hi {employee.display_name},\n\n{text}{tail}\n\n{site_link(link)}\n"
        subject = f"{settings.STACKS_SITE_NAME}: shift request {'approved' if approved else 'not approved'}"
        notify(employee.user, ex.organization, NotificationCategory.EXCHANGE, subject, body, link)
