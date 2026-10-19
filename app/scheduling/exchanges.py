# SPDX-License-Identifier: AGPL-3.0-or-later
"""Claiming open shifts, offering shifts for cover, and two-way swaps (SPEC §9.2, D104).

Employees only ever act on the published schedule. A request names a shift as it was
published; approval re-checks that the working copy still matches, runs the validation suite
on the would-be result, and reassigns the shift (both shifts, for a swap) as drafts. The
people who asked hear the decision (D103); the change reaches the published schedule, and the
schedule notifications, when a supervisor publishes (CLAUDE.md §2.2). The scheduling pool
doesn't limit who may take a shift here: it limits only the generator and suggestions (D104).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

import pghistory
from django.db import transaction
from django.db.models import Q
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


def _overlaps_own(employee: Employee, shift: Shift, ignoring: tuple[int, ...] = ()) -> bool:
    return any(
        s.interval.overlaps(shift.interval)
        for s in Shift.objects.live()
        .filter(employee=employee, date=shift.date)
        .exclude(pk__in=(shift.pk, *ignoring))
    )


def _busy(*shifts: Shift) -> bool:
    """Any of these shifts is already in an active request, on either side of a swap."""
    ids = [s.pk for s in shifts]
    return ShiftExchange.objects.filter(
        Q(shift__in=ids) | Q(other_shift__in=ids), status__in=ACTIVE_EXCHANGE
    ).exists()


def _when(shift: Shift) -> str:
    return f"{shift.date:%a %-d %b} {shift.interval.label()}"


def _within(departments: frozenset[int] | None, shift: Shift, what: str) -> None:
    if departments is not None and shift.period.department_id not in departments:
        raise ExchangeError(f"That {what} isn't in your departments.")


def claim(employee: Employee, shift: Shift, today: date) -> ShiftExchange:
    from app.scheduling.published import open_shift_departments

    _future(shift, today)
    _within(open_shift_departments(employee), shift, "open shift")
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
    if _busy(shift):
        raise ExchangeError("That shift is already offered or in a swap request.")
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
        from app.scheduling.published import student_departments

        _within(student_departments(employee), ex.shift, "shift")
        if _overlaps_own(employee, ex.shift):
            raise ExchangeError("You're already working at that time.")
        ex.taker = employee
        ex.status = ExchangeStatus.PENDING
        ex.save()
    return ex


def swap(employee: Employee, shift: Shift, other: Shift, today: date) -> ShiftExchange:
    """Propose swapping one of your published shifts for a co-worker's (D104). The co-worker
    accepts or declines; then a supervisor decides."""
    from app.scheduling.published import student_departments

    _future(shift, today)
    _future(other, today)
    if not _matches_published(shift, employee.pk):
        raise ExchangeError("You can only swap your own shifts from the published schedule.")
    _within(student_departments(employee), other, "shift")
    coworker = other.employee
    if coworker is None or coworker.pk == employee.pk or not _matches_published(other, coworker.pk):
        raise ExchangeError("That shift isn't a co-worker's shift on the published schedule any more.")
    if coworker.archived_at is not None or coworker.user_id is None:
        raise ExchangeError("That co-worker can't take part in swaps.")
    if _overlaps_own(employee, other, ignoring=(shift.pk,)):
        raise ExchangeError("You're already working at the time of their shift.")
    if _overlaps_own(coworker, shift, ignoring=(other.pk,)):
        raise ExchangeError("They're already working at the time of your shift.")
    if _busy(shift, other):
        raise ExchangeError("One of those shifts is already offered or in a swap request.")
    ex = ShiftExchange.objects.create(
        organization=employee.organization,
        shift=shift,
        other_shift=other,
        kind=ExchangeKind.SWAP,
        status=ExchangeStatus.ASKED,
        requester=employee,
        taker=coworker,
    )
    _tell(
        coworker,
        ex,
        "a co-worker asked to swap shifts",
        f"A co-worker has asked to swap shifts with you: your shift {_when(other)} for theirs {_when(shift)}. "
        "Accept or decline it on the open shifts and cover page.",
    )
    return ex


def respond(employee: Employee, ex: ShiftExchange, accept: bool, today: date) -> ShiftExchange:
    """The co-worker accepts a swap, sending it to a supervisor, or declines it."""
    with transaction.atomic():
        ex = ShiftExchange.objects.select_for_update().get(pk=ex.pk)
        if ex.kind != ExchangeKind.SWAP or ex.taker_id != employee.pk:
            raise ExchangeError("That isn't a swap request for you.")
        if ex.status != ExchangeStatus.ASKED:
            raise ExchangeError("That swap request has already been answered or withdrawn.")
        if accept:
            other = ex.other_shift
            if other is None:
                raise ExchangeError("That swap request is missing its second shift.")
            _future(ex.shift, today)
            _future(other, today)
            if not (_matches_published(ex.shift, ex.requester_id) and _matches_published(other, employee.pk)):
                raise ExchangeError("One of the shifts has changed since the swap was proposed.")
            ex.status = ExchangeStatus.PENDING
        else:
            ex.status = ExchangeStatus.DECLINED
        ex.save()
    if not accept:
        _tell(
            ex.requester,
            ex,
            "swap declined",
            f"Your co-worker declined the swap of your shift {_when(ex.shift)}. The shift is still yours.",
        )
    return ex


def withdraw(employee: Employee, ex: ShiftExchange) -> ShiftExchange:
    """The requester withdraws the whole request; a volunteer withdraws only themselves,
    which puts the offer back up; the other side of a swap backs out, which declines it."""
    with transaction.atomic():
        ex = ShiftExchange.objects.select_for_update().get(pk=ex.pk)
        if ex.status not in ACTIVE_EXCHANGE:
            raise ExchangeError("That request has already been decided.")
        if ex.kind == ExchangeKind.COVER and ex.taker_id == employee.pk and ex.requester_id != employee.pk:
            ex.taker = None
            ex.status = ExchangeStatus.OPEN
        elif ex.kind == ExchangeKind.SWAP and ex.taker_id == employee.pk:
            ex.status = ExchangeStatus.DECLINED
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


def _moves(ex: ShiftExchange) -> dict[int, tuple[Shift, int | None, int]] | None:
    """Shift id to (shift, who holds it now, who gets it), or None if a shift has moved since."""
    if ex.taker_id is None:
        return None
    holder = None if ex.kind == ExchangeKind.CLAIM else ex.requester_id
    moves = {ex.shift_id: (ex.shift, holder, ex.taker_id)}
    if ex.kind == ExchangeKind.SWAP:
        if ex.other_shift is None:
            return None
        moves[ex.other_shift_id] = (ex.other_shift, ex.taker_id, ex.requester_id)
    for shift, before, _ in moves.values():
        if shift.deleted_at is not None or shift.employee_id != before:
            return None
    return moves


def review(ex: ShiftExchange) -> Review:
    """What approving would do: the validation suite on the would-be result (SPEC §9.2). For a
    swap, both moves are checked together, period by period."""
    moves = _moves(ex)
    if moves is None:
        return Review(ex, False, None)
    errors: list[str] = []
    warnings: list[str] = []
    periods = {shift.period_id: shift.period for shift, _, _ in moves.values()}
    for period in periods.values():
        ctx = services.build_context(period)
        after = [replace(s, employee_id=moves[s.id][2]) if s.id in moves else s for s in ctx.shifts]
        check = suggest.check_change(ctx, after, pool_rule=False)
        errors.extend(check.errors)
        warnings.extend(check.warnings)
    return Review(ex, True, suggest.ChangeCheck(tuple(dict.fromkeys(errors)), tuple(dict.fromkeys(warnings))))


def decide(ex: ShiftExchange, user, approve: bool) -> ShiftExchange:
    with transaction.atomic(), pghistory.context(action="shift_exchange_decision", exchange=ex.pk):
        # Lock the request row alone: Postgres can't lock through the nullable joins.
        ex = ShiftExchange.objects.select_for_update().get(pk=ex.pk)
        if ex.status != ExchangeStatus.PENDING:
            raise ExchangeError("That request isn't waiting for a decision.")
        if approve:
            if ex.taker is None:
                raise ExchangeError("Nobody has volunteered for that shift yet.")
            ids = [i for i in (ex.shift_id, ex.other_shift_id) if i is not None]
            locked = {s.pk: s for s in Shift.objects.select_for_update().filter(pk__in=ids).order_by()}
            ex.shift = locked[ex.shift_id]
            if ex.other_shift_id is not None:
                ex.other_shift = locked[ex.other_shift_id]
            result = review(ex)
            if not result.still_matches:
                raise ExchangeError(
                    "A shift has changed since the request was made. Deny it, or edit the shift."
                )
            if result.check is not None and not result.check.ok:
                raise ExchangeError("Approving would break a rule: " + "; ".join(result.check.errors))
            people = {ex.taker_id: ex.taker, ex.requester_id: ex.requester}
            for shift, _, to in (_moves(ex) or {}).values():
                getter = people[to]
                shift.employee = getter
                shift.pay_role = getter.default_pay_role or shift.pay_role
                shift.updated_by = user
                shift.save()
            ShiftExchange.objects.filter(
                Q(shift__in=ids) | Q(other_shift__in=ids), status__in=ACTIVE_EXCHANGE
            ).exclude(pk=ex.pk).update(
                status=ExchangeStatus.DENIED, decided_by=user, decided_at=timezone.now()
            )
        ex.status = ExchangeStatus.APPROVED if approve else ExchangeStatus.DENIED
        ex.decided_by = user
        ex.decided_at = timezone.now()
        ex.save()
        _tell_people(ex)
    return ex


def _tell_people(ex: ShiftExchange) -> None:
    """Tell the people who asked the outcome. No co-workers' names: only the shifts."""
    shift = ex.shift
    when = _when(shift)
    approved = ex.status == ExchangeStatus.APPROVED
    word = "approved" if approved else "not approved"
    lines = []
    if ex.kind == ExchangeKind.SWAP and ex.other_shift is not None:
        theirs = _when(ex.other_shift)
        lines.append((ex.requester, f"Your swap of your shift {when} for {theirs} was {word}."))
        lines.append((ex.taker, f"The swap of your shift {theirs} for {when} was {word}."))
    elif ex.kind == ExchangeKind.CLAIM:
        lines.append(
            (
                ex.taker,
                f"Your request for the open shift {when} was {word}.",
            )
        )
    else:
        if ex.taker is not None:
            lines.append((ex.taker, f"Your offer to cover {when} was {word}."))
        lines.append(
            (
                ex.requester,
                f"Cover for your shift {when} was {'approved: someone else will work it' if approved else 'not approved: the shift is still yours'}.",
            )
        )
    tail = " It shows on the schedule once a supervisor publishes it." if approved else ""
    for employee, text in lines:
        if employee is not None:
            _tell(employee, ex, f"shift request {word}", text + tail)


def _tell(employee: Employee, ex: ShiftExchange, subject: str, text: str) -> None:
    """One in-app notification, by email only if the person turned it on (D103)."""
    from django.conf import settings
    from django.urls import reverse

    from app.scheduling.models import NotificationCategory
    from app.scheduling.notify import notify, site_link

    if employee.user is None:
        return
    link = reverse("shift_exchange")
    body = f"Hi {employee.display_name},\n\n{text}\n\n{site_link(link)}\n"
    notify(
        employee.user,
        ex.organization,
        NotificationCategory.EXCHANGE,
        f"{settings.STACKS_SITE_NAME}: {subject}",
        body,
        link,
    )
