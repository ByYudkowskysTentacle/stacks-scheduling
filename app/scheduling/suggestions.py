# SPDX-License-Identifier: AGPL-3.0-or-later
"""Database side of the suggestion queries and remove-from-pool (domain/suggest.py).

Accepting a suggestion is an ordinary draft edit: one shift reassigned or added, audited,
nothing sent until a supervisor publishes. A reassigned shift takes the new person's default
pay role (scenario 15).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

import pghistory
from django.db import transaction

from app.people.models import Employee
from app.scheduling import services
from app.scheduling.models import SchedulePeriod, Shift
from domain import suggest
from domain.timeutil import Interval
from domain.validation import Report


class SuggestionError(Exception):
    pass


def _domain_shift(ctx, shift: Shift):
    return next(s for s in ctx.shifts if s.id == shift.pk)


def takers_for_shift(period: SchedulePeriod, shift: Shift) -> suggest.TakerResult:
    ctx = services.build_context(period)
    return suggest.who_could_take(ctx, _domain_shift(ctx, shift))


def takers_for_gap(period: SchedulePeriod, day: date, interval: Interval) -> suggest.TakerResult:
    ctx = services.build_context(period)
    return suggest.who_could_take(ctx, suggest.gap_shift(day, interval, period.location_id))


def gap_interval(period: SchedulePeriod, day: date, interval: Interval) -> Interval:
    return suggest.widen_gap(services.build_context(period), day, interval)


def opportunities_for(period: SchedulePeriod, employee: Employee) -> suggest.OpportunityResult:
    ctx = services.build_context(period)
    if employee.pk not in ctx.people:
        raise SuggestionError(f"{employee.display_name} isn't on this schedule's roster.")
    return suggest.opportunities(ctx, employee.pk)


def _check_eligible(result: suggest.TakerResult, employee: Employee) -> None:
    taker = next((t for t in result.takers if t.employee_id == employee.pk), None)
    if taker is None or not taker.eligible:
        why = "; ".join(taker.blockers) if taker else "not on this schedule's roster"
        raise SuggestionError(f"{employee.display_name} can't take this shift: {why}.")


def give_shift(period: SchedulePeriod, shift: Shift, employee: Employee, user) -> Shift:
    """Reassign an existing draft shift after re-checking the person is still eligible."""
    with transaction.atomic(), pghistory.context(action="suggestion_reassign"):
        shift = Shift.objects.select_for_update().get(pk=shift.pk, deleted_at__isnull=True)
        _check_eligible(takers_for_shift(period, shift), employee)
        shift.employee = employee
        shift.pay_role = employee.default_pay_role or shift.pay_role
        shift.updated_by = user
        shift.proposed_by_lead = False
        shift.save()
    return shift


def cover_gap(period: SchedulePeriod, day: date, interval: Interval, employee: Employee, user) -> Shift:
    """Add a draft shift for `employee` over a gap after checking they're eligible."""
    with transaction.atomic(), pghistory.context(action="suggestion_cover_gap"):
        _check_eligible(takers_for_gap(period, day, interval), employee)
        return Shift.objects.create(
            organization=period.organization,
            location=period.location,
            period=period,
            employee=employee,
            date=day,
            start_min=interval.start,
            end_min=interval.end,
            pay_role=employee.default_pay_role,
            updated_by=user,
        )


# --- Remove from pool -----------------------------------------------------------------------


@dataclass
class PeriodPlan:
    period: SchedulePeriod
    moves: list[suggest.Reassignment]


def pool_plan(
    employee: Employee, from_day: date, department_ids: frozenset[int] | None = None
) -> list[PeriodPlan]:
    """For each period with the employee's live shifts on or after `from_day`, who takes each.
    `department_ids` limits it to the schedules a supervisor manages (D95)."""
    periods = (
        SchedulePeriod.objects.filter(
            organization=employee.organization,
            shifts__employee=employee,
            shifts__deleted_at__isnull=True,
            shifts__date__gte=from_day,
        )
        .distinct()
        .select_related("location", "organization")
        .order_by("start_date")
    )
    if department_ids is not None:
        periods = periods.filter(department_id__in=department_ids)
    out = []
    for period in periods:
        ctx = services.build_context(period)
        moves = suggest.remove_from_pool_plan(ctx, employee.pk, from_day)
        if moves:
            out.append(PeriodPlan(period, moves))
    return out


def remove_from_pool(
    employee: Employee, user, from_day: date, department_ids: frozenset[int] | None = None
) -> list[tuple[SchedulePeriod, Report]]:
    """Take the employee out of the scheduling pool and reassign their shifts from `from_day`
    as drafts. Nothing is deleted: a shift nobody can take becomes an open shift. Returns the
    validation report for each period touched (CLAUDE.md §2.4)."""
    with transaction.atomic(), pghistory.context(action="remove_from_pool", employee=employee.pk):
        employee = Employee.objects.select_for_update().get(pk=employee.pk)
        plans = pool_plan(employee, from_day, department_ids)
        employee.in_scheduling_pool = False
        employee.save(update_fields=["in_scheduling_pool"])
        newcomers = {
            e.pk: e
            for e in Employee.objects.filter(
                pk__in={m.to_id for p in plans for m in p.moves if m.to_id is not None}
            ).select_related("default_pay_role")
        }
        for plan in plans:
            for move in plan.moves:
                shift = Shift.objects.select_for_update().get(pk=move.shift.id)
                to = newcomers.get(move.to_id) if move.to_id is not None else None
                shift.employee = to
                if to is not None and to.default_pay_role_id:
                    shift.pay_role = to.default_pay_role
                shift.updated_by = user
                shift.save()
    return [(plan.period, services.validate_period(plan.period)) for plan in plans]


# --- Targeted move and split ----------------------------------------------------------------


def plan_move(
    period: SchedulePeriod,
    recipient: Employee,
    donors: list[Employee],
    minutes: int,
    weeks: frozenset[int] | None,
) -> suggest.TargetedMove:
    ctx = services.build_context(period)
    if recipient.pk not in ctx.people:
        raise SuggestionError(f"{recipient.display_name} isn't on this schedule's roster.")
    return suggest.targeted_move(ctx, recipient.pk, [d.pk for d in donors], minutes, weeks)


def apply_move(period: SchedulePeriod, recipient: Employee, shift_ids: list[int], user) -> list[Shift]:
    """Reassign the chosen shifts to `recipient` together, after checking them together."""
    with transaction.atomic(), pghistory.context(action="targeted_move", employee=recipient.pk):
        rows = list(period.shifts.live().select_for_update(of=("self",)).filter(pk__in=shift_ids))
        if not rows or len(rows) != len(set(shift_ids)):
            raise SuggestionError("Those shifts have changed since the plan was made. Plan the move again.")
        ctx = services.build_context(period)
        moving = set(shift_ids)
        after = [replace(s, employee_id=recipient.pk) if s.id in moving else s for s in ctx.shifts]
        check = suggest.check_change(ctx, after)
        if not check.ok:
            raise SuggestionError("The move no longer passes: " + "; ".join(check.errors))
        for shift in rows:
            shift.employee = recipient
            shift.pay_role = recipient.default_pay_role or shift.pay_role
            shift.updated_by = user
            shift.proposed_by_lead = False
            shift.save()
    return rows


def apply_split_off(
    period: SchedulePeriod, shift_id: int, start: int, end: int, recipient: Employee, user
) -> tuple[Shift, Shift]:
    """Give the start or the end of one shift to `recipient` (D102). The holder keeps the rest on
    the original shift, so its ID and history survive; the part becomes a new draft shift."""
    with transaction.atomic(), pghistory.context(action="targeted_move_split", employee=recipient.pk):
        shift = period.shifts.live().select_for_update(of=("self",)).filter(pk=shift_id).first()
        if shift is None or not (
            (start == shift.start_min and end < shift.end_min)
            or (end == shift.end_min and start > shift.start_min)
        ):
            raise SuggestionError("That shift has changed since the plan was made. Plan the move again.")
        ctx = services.build_context(period)
        _, check = suggest.split_off(ctx, _domain_shift(ctx, shift), Interval(start, end), recipient.pk)
        if not check.ok:
            raise SuggestionError("The split no longer passes: " + "; ".join(check.errors))
        if start == shift.start_min:
            shift.start_min = end
        else:
            shift.end_min = start
        shift.updated_by = user
        shift.save()
        added = Shift.objects.create(
            organization=period.organization,
            location=shift.location,
            period=period,
            employee=recipient,
            date=shift.date,
            start_min=start,
            end_min=end,
            pay_role=recipient.default_pay_role or shift.pay_role,
            updated_by=user,
        )
    return shift, added


def plan_split(period: SchedulePeriod, shift: Shift, at: int) -> suggest.SplitPlan:
    ctx = services.build_context(period)
    return suggest.split_shift(ctx, _domain_shift(ctx, shift), at)


def apply_split(
    period: SchedulePeriod, shift: Shift, at: int, first: Employee | None, second: Employee | None, user
) -> tuple[Shift, Shift]:
    """Retime the original shift to the first part (keeping its ID and history) and add the
    second part as a new draft shift (CLAUDE.md §2.3)."""
    with transaction.atomic(), pghistory.context(action="split_shift", shift=shift.pk):
        shift = Shift.objects.select_for_update().get(pk=shift.pk, deleted_at__isnull=True)
        ctx = services.build_context(period)
        try:
            _, check = suggest.split_result(
                ctx, _domain_shift(ctx, shift), at, first.pk if first else None, second.pk if second else None
            )
        except ValueError as exc:
            raise SuggestionError(str(exc).capitalize() + ".") from exc
        if not check.ok:
            raise SuggestionError("The split doesn't pass: " + "; ".join(check.errors))
        end = shift.end_min
        holder = shift.employee_id
        shift.end_min = at
        shift.employee = first
        if first is not None and first.pk != holder:
            shift.pay_role = first.default_pay_role or shift.pay_role
        shift.updated_by = user
        shift.save()
        added = Shift.objects.create(
            organization=period.organization,
            location=shift.location,
            period=period,
            employee=second,
            date=shift.date,
            start_min=at,
            end_min=end,
            pay_role=(second.default_pay_role if second else None) or shift.pay_role,
            updated_by=user,
        )
    return shift, added
