# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView

from app.core.forms import DateInput
from app.core.permissions import Perm
from app.people.models import Employee
from app.scheduling import services, solver
from app.scheduling.models import RunStatus, SchedulingRules
from app.scheduling.views import PeriodMixin, flash_validation


class SolveForm(forms.Form):
    mode = forms.ChoiceField(
        choices=[
            ("rebalance", "Rebalance: improve the current schedule with as few changes as possible"),
            (
                "generate",
                "Generate: build the period from scratch (current drafts are replaced if you apply it)",
            ),
        ],
        widget=forms.RadioSelect,
    )
    change_limit_percent = forms.IntegerField(
        min_value=0, max_value=100, label="Change limit for the limited option (% of scheduled hours)"
    )
    allow_published = forms.BooleanField(
        required=False, label="Allow changes to published shifts for this run"
    )
    freeze_hours = forms.IntegerField(min_value=0, max_value=24 * 14, label="Freeze window (hours from now)")
    first_day = forms.DateField(required=False, widget=DateInput, label="Only change from")
    last_day = forms.DateField(required=False, widget=DateInput, label="Only change until")
    people = forms.ModelMultipleChoiceField(
        queryset=Employee.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Leave all unticked to include everyone. Hours from the people ticked may go to anyone eligible.",
    )

    def __init__(self, *args, period, **kwargs):
        super().__init__(*args, **kwargs)
        self.period = period
        self.fields["people"].queryset = services.employees_for_grid(period).order_by("preferred_name")
        from app.scheduling.forms import label_with_hours, minutes_by_person

        self.fields["people"].label_from_instance = label_with_hours(minutes_by_person(period))
        self.fields["first_day"].widget.attrs.update(
            min=period.start_date.isoformat(), max=period.end_date.isoformat()
        )
        self.fields["last_day"].widget.attrs.update(
            min=period.start_date.isoformat(), max=period.end_date.isoformat()
        )

    def clean(self):
        cleaned = super().clean()
        for name in ("first_day", "last_day"):
            day = cleaned.get(name)
            if day and not self.period.contains(day):
                self.add_error(name, "Pick a date in this period.")
        if (
            cleaned.get("first_day")
            and cleaned.get("last_day")
            and cleaned["last_day"] < cleaned["first_day"]
        ):
            self.add_error("last_day", "Ends before it starts.")
        return cleaned

    def params(self) -> dict:
        c = self.cleaned_data
        return {
            "change_limit_percent": c["change_limit_percent"],
            "allow_published": c["allow_published"],
            "freeze_hours": c["freeze_hours"],
            "first_day": c["first_day"].isoformat() if c.get("first_day") else None,
            "last_day": c["last_day"].isoformat() if c.get("last_day") else None,
            "people": [e.pk for e in c["people"]],
        }


class SolveStartView(PeriodMixin, View):
    permission = Perm.EDIT_SCHEDULE

    def get(self, request, period_pk):
        period = self.get_period()
        rules = SchedulingRules.for_org(period.organization)
        mode = "generate" if not period.shifts.live().exists() else "rebalance"
        form = SolveForm(
            period=period,
            initial={
                "mode": request.GET.get("mode", mode),
                "change_limit_percent": rules.change_limit_percent,
                "freeze_hours": rules.freeze_window_hours,
            },
        )
        return render(
            request,
            "scheduling/solve_start.html",
            {"period": period, "form": form, "runs": period.solve_runs.all()[:10]},
        )

    def post(self, request, period_pk):
        period = self.get_period()
        form = SolveForm(request.POST, period=period)
        if not form.is_valid():
            return render(
                request,
                "scheduling/solve_start.html",
                {"period": period, "form": form, "runs": period.solve_runs.all()[:10]},
            )
        run = solver.start_solve(period, request.user, form.cleaned_data["mode"], form.params())
        return redirect("solve_run", period_pk=period.pk, pk=run.pk)


class WhatIfView(PeriodMixin, View):
    """One-click what-ifs for unmet rules (D36): re-run with one setting relaxed for this run only."""

    permission = Perm.EDIT_SCHEDULE

    def post(self, request, period_pk, pk):
        period = self.get_period()
        base = get_object_or_404(period.solve_runs, pk=pk)
        params = {k: v for k, v in base.params.items() if k != "start_hash"}
        what_if = request.POST.get("what_if")
        if what_if == "allow_published":
            params["allow_published"] = True
        elif what_if == "shrink_freeze":
            params["freeze_hours"] = 0
        elif what_if == "relax_rule" and request.POST.get("rule"):
            params["relax_tag_rule"] = request.POST["rule"]  # just that hard rule, this run only (D97)
        elif what_if == "relax_rule":
            params["relax_tag_rules"] = True
        else:
            messages.error(request, "Unknown what-if.")
            return redirect("solve_run", period_pk=period.pk, pk=base.pk)
        run = solver.start_solve(period, request.user, base.mode, params)
        return redirect("solve_run", period_pk=period.pk, pk=run.pk)


class SolveRunView(PeriodMixin, TemplateView):
    permission = Perm.EDIT_SCHEDULE
    template_name = "scheduling/solve_run.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        period = self.get_period()
        run = get_object_or_404(period.solve_runs, pk=self.kwargs["pk"])
        ctx.update(
            period=period,
            run=run,
            Status=RunStatus,
            waiting=run.status in (RunStatus.QUEUED, RunStatus.RUNNING),
            options=run.result.get("options", []),
            weeks=range(period.weeks),
        )
        return ctx

    def post(self, request, period_pk, pk):
        period = self.get_period()
        run = get_object_or_404(period.solve_runs, pk=pk)
        action = request.POST.get("action")
        try:
            if action == "apply":
                if request.POST.get("confirm") != "yes":
                    messages.error(request, "Tick the confirmation box to apply the option.")
                    return redirect("solve_run", period_pk=period.pk, pk=run.pk)
                touched = solver.apply_option(run, request.POST.get("option", ""), request.user)
                flash_validation(
                    request,
                    period,
                    f"Applied as a draft: {touched} shift(s) changed. Nothing is published or sent.",
                )
                return redirect(reverse("period", args=[period.pk]) + "#validation")
            if action == "undo":
                if request.POST.get("confirm") != "yes":
                    messages.error(request, "Tick the confirmation box to undo.")
                    return redirect("solve_run", period_pk=period.pk, pk=run.pk)
                solver.undo_apply(run, request.user)
                flash_validation(
                    request, period, "Undone: the schedule is back to how it was before this run."
                )
                return redirect("period", period_pk=period.pk)
        except solver.SolveError as exc:
            messages.error(request, str(exc))
        return redirect("solve_run", period_pk=period.pk, pk=run.pk)
