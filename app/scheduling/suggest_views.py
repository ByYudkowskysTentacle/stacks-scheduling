# SPDX-License-Identifier: AGPL-3.0-or-later
"""Suggestion queries, one click from the schedule (SPEC §7.5), and remove-from-pool (§7.3)."""

from __future__ import annotations

from datetime import date

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from app.core.permissions import Perm
from app.core.views_base import OrgViewMixin
from app.people.models import Employee
from app.scheduling import suggestions
from app.scheduling.forms import SplitForm, TargetedMoveForm
from app.scheduling.models import Shift
from app.scheduling.views import PeriodMixin, flash_validation
from domain.timeutil import Interval, is_on_slot
from domain.validation import day_label


def _week_url(period, day: date) -> str:
    return reverse("period", args=[period.pk]) + f"?week={period.days.index(day) // 7}"


class ShiftTakersView(PeriodMixin, View):
    """Who could take this shift?"""

    permission = Perm.EDIT_SCHEDULE

    def get_shift(self, period) -> Shift:
        return get_object_or_404(period.shifts.live().select_related("employee"), pk=self.kwargs["pk"])

    def get(self, request, period_pk, pk):
        period = self.get_period()
        shift = self.get_shift(period)
        result = suggestions.takers_for_shift(period, shift)
        return render(
            request,
            "scheduling/takers.html",
            {
                "period": period,
                "shift": shift,
                "result": result,
                "title": f"Who could take {shift.employee.display_name if shift.employee else 'this open shift'}'s "
                f"shift, {day_label(shift.date)} {shift.interval.label()}?",
                "action_label": "Give it to",
            },
        )

    def post(self, request, period_pk, pk):
        period = self.get_period()
        shift = self.get_shift(period)
        employee = get_object_or_404(self.org_queryset(Employee), pk=request.POST.get("employee"))
        try:
            suggestions.give_shift(period, shift, employee, request.user)
        except suggestions.SuggestionError as exc:
            messages.error(request, str(exc))
            return redirect("shift_takers", period_pk=period.pk, pk=shift.pk)
        flash_validation(request, period, f"Gave the shift to {employee.display_name} as a draft.")
        return redirect(_week_url(period, shift.date))


class GapTakersView(PeriodMixin, View):
    """Who could cover this gap?"""

    permission = Perm.EDIT_SCHEDULE

    def parse(self, period, data) -> tuple[date, Interval]:
        try:
            day = date.fromisoformat(data.get("date", ""))
            start, end = int(data.get("start", "")), int(data.get("end", ""))
            interval = Interval(start, end)
        except ValueError as exc:
            raise Http404 from exc
        if not period.contains(day) or not (is_on_slot(start) and is_on_slot(end)):
            raise Http404
        return day, interval

    def get(self, request, period_pk):
        period = self.get_period()
        day, gap = self.parse(period, request.GET)
        interval = suggestions.gap_interval(period, day, gap)
        result = suggestions.takers_for_gap(period, day, interval)
        return render(
            request,
            "scheduling/takers.html",
            {
                "period": period,
                "gap": {"date": day.isoformat(), "start": interval.start, "end": interval.end},
                "result": result,
                "title": f"Who could cover {day_label(day)} {interval.label()}?",
                "widened_from": gap.label() if gap != interval else "",
                "action_label": "Add a shift for",
            },
        )

    def post(self, request, period_pk):
        period = self.get_period()
        day, interval = self.parse(period, request.POST)
        employee = get_object_or_404(self.org_queryset(Employee), pk=request.POST.get("employee"))
        try:
            suggestions.cover_gap(period, day, interval, employee, request.user)
        except suggestions.SuggestionError as exc:
            messages.error(request, str(exc))
            url = reverse("gap_takers", args=[period.pk])
            return redirect(f"{url}?date={day.isoformat()}&start={interval.start}&end={interval.end}")
        flash_validation(request, period, f"Added a draft shift for {employee.display_name}.")
        return redirect(_week_url(period, day))


class RaiseHoursView(PeriodMixin, View):
    """How can I raise this person's hours?"""

    permission = Perm.EDIT_SCHEDULE

    def get(self, request, period_pk, employee_pk):
        period = self.get_period()
        employee = get_object_or_404(self.org_queryset(Employee), pk=employee_pk)
        try:
            result = suggestions.opportunities_for(period, employee)
        except suggestions.SuggestionError as exc:
            raise Http404(str(exc)) from exc
        weeks = [
            {
                "label": f"Week {w + 1}",
                "minutes": result.week_minutes[w],
                "low_threshold": result.low_threshold.get(w, 0),
                "items": result.by_week.get(w, []),
            }
            for w in range(period.weeks)
        ]
        return render(
            request,
            "scheduling/raise_hours.html",
            {"period": period, "employee": employee, "result": result, "weeks": weeks},
        )

    def post(self, request, period_pk, employee_pk):
        period = self.get_period()
        employee = get_object_or_404(self.org_queryset(Employee), pk=employee_pk)
        shift = get_object_or_404(period.shifts.live(), pk=request.POST.get("shift"))
        try:
            suggestions.give_shift(period, shift, employee, request.user)
        except suggestions.SuggestionError as exc:
            messages.error(request, str(exc))
        else:
            flash_validation(request, period, f"Gave {employee.display_name} the {shift} shift as a draft.")
        return redirect("raise_hours", period_pk=period.pk, employee_pk=employee.pk)


class RemoveFromPoolView(OrgViewMixin, View):
    """Take someone out of the scheduling pool and reassign their upcoming shifts (scenario 9).
    Shows the plan first; nothing changes until it's confirmed."""

    permission = Perm.MANAGE_PEOPLE

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not request.access.has(Perm.EDIT_SCHEDULE):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, pk):
        employee = get_object_or_404(self.org_queryset(Employee), pk=pk)
        today = timezone.localdate()
        return render(
            request,
            "people/remove_from_pool.html",
            {"employee": employee, "plans": suggestions.pool_plan(employee, today), "today": today},
        )

    def post(self, request, pk):
        employee = get_object_or_404(self.org_queryset(Employee), pk=pk)
        if request.POST.get("confirm") != "yes":
            messages.error(request, "Tick the confirmation box to go ahead.")
            return redirect("remove_from_pool", pk=pk)
        results = suggestions.remove_from_pool(employee, request.user, timezone.localdate())
        moved = "" if not results else " Their upcoming shifts were reassigned as drafts."
        messages.success(request, f"Took {employee.display_name} out of the scheduling pool.{moved}")
        for period, report in results:
            level = messages.SUCCESS if not report.counted else messages.WARNING
            messages.add_message(request, level, f"Schedule {period}: validation {report.summary()}.")
        return redirect("employee_detail", pk=pk)


class TargetedMoveView(PeriodMixin, View):
    """Move about N hours from some people to one person (SPEC §7.3, scenario 10). Planning is
    a what-if; applying reassigns the chosen whole shifts, or splits one shift (D102), as
    drafts."""

    permission = Perm.EDIT_SCHEDULE

    def get(self, request, period_pk):
        period = self.get_period()
        form = TargetedMoveForm(request.GET or None, period=period)
        move = None
        if form.is_bound and form.is_valid():
            d = form.cleaned_data
            weeks = frozenset({d["week"]}) if d["week"] is not None else None
            try:
                move = suggestions.plan_move(
                    period, d["recipient"], list(d["donors"]), int(d["hours"] * 60), weeks
                )
            except suggestions.SuggestionError as exc:
                form.add_error(None, str(exc))
        return render(
            request, "scheduling/targeted_move.html", {"period": period, "form": form, "move": move}
        )

    def post(self, request, period_pk):
        period = self.get_period()
        recipient = get_object_or_404(self.org_queryset(Employee), pk=request.POST.get("recipient"))
        if request.POST.get("split"):
            try:
                kept, added = suggestions.apply_split_off(
                    period,
                    int(request.POST["split"]),
                    int(request.POST.get("start", "")),
                    int(request.POST.get("end", "")),
                    recipient,
                    request.user,
                )
            except (ValueError, suggestions.SuggestionError) as exc:
                messages.error(request, str(exc))
                return redirect("targeted_move", period_pk=period.pk)
            flash_validation(
                request,
                period,
                f"Split the shift: {added.minutes / 60:g}h to {recipient.display_name}, "
                f"the rest stays with {kept.employee.display_name if kept.employee else 'nobody'}, as drafts.",
            )
            return redirect(reverse("period", args=[period.pk]))
        try:
            ids = [int(x) for x in request.POST.getlist("shift")]
            rows = suggestions.apply_move(period, recipient, ids, request.user)
        except (ValueError, suggestions.SuggestionError) as exc:
            messages.error(request, str(exc))
            return redirect("targeted_move", period_pk=period.pk)
        total = sum(s.minutes for s in rows) / 60
        flash_validation(request, period, f"Moved {total:g}h to {recipient.display_name} as drafts.")
        return redirect(reverse("period", args=[period.pk]))


class SplitShiftView(PeriodMixin, View):
    """Split one shift into two parts and assign each (SPEC §7.3, scenario 11)."""

    permission = Perm.EDIT_SCHEDULE

    def get_shift(self, period) -> Shift:
        return get_object_or_404(period.shifts.live().select_related("employee"), pk=self.kwargs["pk"])

    def get(self, request, period_pk, pk):
        period = self.get_period()
        shift = self.get_shift(period)
        midpoint = (shift.start_min + shift.end_min) // 2 // 15 * 15
        form = SplitForm(request.GET or None, initial={"at": midpoint})
        at = form.cleaned_data["at"] if form.is_bound and form.is_valid() else midpoint
        plan = None
        try:
            plan = suggestions.plan_split(period, shift, at)
        except ValueError:
            form.add_error("at", "Choose a time inside the shift.")
        return render(
            request,
            "scheduling/split.html",
            {"period": period, "shift": shift, "form": form, "plan": plan, "at": at},
        )

    def post(self, request, period_pk, pk):
        period = self.get_period()
        shift = self.get_shift(period)
        people = self.org_queryset(Employee)
        first = people.filter(pk=request.POST.get("first") or 0).first()
        second = people.filter(pk=request.POST.get("second") or 0).first()
        try:
            at = int(request.POST.get("at", ""))
            first_part, second_part = suggestions.apply_split(period, shift, at, first, second, request.user)
        except (ValueError, suggestions.SuggestionError) as exc:
            messages.error(request, str(exc))
            return redirect("split_shift", period_pk=period.pk, pk=shift.pk)
        flash_validation(request, period, f"Split into {first_part} and {second_part} as drafts.")
        return redirect(_week_url(period, shift.date))
