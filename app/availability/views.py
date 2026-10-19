# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from datetime import date, timedelta

from django.contrib import messages
from django.db.models import Prefetch
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.generic import FormView, TemplateView, View

from app.availability import time_off
from app.availability.forms import TimeOffForm, WeeklyAvailabilityForm
from app.availability.models import NOT_ENTERED, Availability, Status, TimeOff, TimeOffStatus
from app.availability.services import (
    confirm_availability,
    grid_range,
    representative_week,
    save_availability,
    week_summaries,
)
from app.core.models import Location, Term
from app.core.permissions import Perm
from app.core.views_base import OrgViewMixin
from app.people.models import Employee, EmployeeTag, Tag
from domain import calendar as cal
from domain.availability import coverage_summary
from domain.timeutil import Interval, intersect


class TermMixin(OrgViewMixin):
    """Picks the term from `?term=` (scoped to the organization) or the current one."""

    def get_term(self) -> Term:
        terms = self.org_queryset(Term)
        if pk := self.request.GET.get("term") or self.request.POST.get("term"):
            return get_object_or_404(terms, pk=pk)
        term = Term.current_for(self.request.organization, timezone.localdate())
        if term is None:
            raise Http404("No terms have been set up yet.")
        return term

    def get_location(self) -> Location | None:
        return self.org_queryset(Location).filter(is_active=True).first()


class AvailabilityEditMixin(TermMixin, FormView):
    form_class = WeeklyAvailabilityForm
    template_name = "availability/edit.html"

    def get_employee(self) -> Employee:
        raise NotImplementedError

    def setup_objects(self):
        self.employee = self.get_employee()
        self.term = self.get_term()
        self.availability = (
            Availability.objects.filter(employee=self.employee, term=self.term)
            .prefetch_related("blocks")
            .first()
        )

    def get(self, request, *args, **kwargs):
        self.setup_objects()
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        self.setup_objects()
        return super().post(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["week_start"] = self.request.organization.week_start
        kwargs["initial_availability"] = self.availability.to_domain() if self.availability else NOT_ENTERED
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        org = self.request.organization
        week = representative_week(org, self.term, timezone.localdate())
        rng = grid_range(self.get_location(), week)
        ctx.update(
            employee=self.employee,
            term=self.term,
            terms=self.org_queryset(Term),
            availability=self.availability,
            status=self.availability.get_status_display() if self.availability else "Not entered",
            needs_review=bool(self.availability and self.availability.status == Status.NEEDS_REVIEW),
            grid={
                "start": rng.start,
                "end": rng.end,
                "weekdays": cal.ordered_weekdays(org.week_start),
                "names": list(cal.WEEKDAY_SHORT),
                "open": {wd: [[iv.start, iv.end] for iv in ivs] for wd, ivs in rng.open_by_weekday.items()},
            },
        )
        if self.employee == self.request.access.employee:
            from app.scheduling.forms import DesiredHoursForm, desired_initial
            from app.scheduling.models import DesiredHours

            desired = DesiredHours.objects.filter(employee=self.employee, term=self.term).first()
            ctx["desired"] = desired
            ctx["desired_form"] = DesiredHoursForm(initial=desired_initial(desired))
        if self.availability and (location := self.get_location()):
            ctx["summary"] = week_summaries([self.employee], self.term, location, week)[self.employee.pk]
        return ctx

    def form_valid(self, form):
        save_availability(self.employee, self.term, form.blocks, self.request.user)
        messages.success(self.request, f"Availability saved for {self.term.name}.")
        return redirect(self.success_url_for())

    def success_url_for(self) -> str:
        raise NotImplementedError


class MyAvailabilityView(AvailabilityEditMixin):
    """An employee's own weekly availability."""

    def get_employee(self) -> Employee:
        employee = self.request.access.employee
        if employee is None:
            raise Http404("Your account isn't linked to an employee record.")
        return employee

    def success_url_for(self) -> str:
        return f"{self.request.path}?term={self.term.pk}"


class EmployeeAvailabilityView(AvailabilityEditMixin):
    """A supervisor editing on an employee's behalf (SPEC §5.2). The audit log records who."""

    permission = Perm.MANAGE_PEOPLE

    def get_employee(self) -> Employee:
        return get_object_or_404(self.org_queryset(Employee).current(), pk=self.kwargs["pk"])

    def success_url_for(self) -> str:
        return f"{self.request.path}?term={self.term.pk}"


class ConfirmAvailabilityView(TermMixin, View):
    """Confirm imported availability (`needs_review` to `entered`), by the employee or a supervisor."""

    def post(self, request, pk):
        availability = get_object_or_404(
            Availability.objects.filter(organization=request.organization), pk=pk
        )
        own = request.access.employee is not None and availability.employee_id == request.access.employee.pk
        if not (own or request.access.has(Perm.MANAGE_PEOPLE)):
            raise Http404
        confirm_availability(availability, request.user)
        messages.success(request, "Availability confirmed.")
        next_url = request.POST.get("next", "")
        if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
            next_url = "my_availability"
        return redirect(next_url)


class AvailabilityGridView(TermMixin, TemplateView):
    """The printable availability grid (SPEC §5.3): one row per employee, one column per day."""

    permission = Perm.VIEW_FREE_BUSY
    template_name = "availability/grid.html"

    def get_week(self, term: Term) -> date:
        org = self.request.organization
        if raw := self.request.GET.get("week"):
            try:
                return cal.week_start_for(date.fromisoformat(raw), org.week_start)
            except ValueError:
                pass
        return representative_week(org, term, timezone.localdate())

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        access = self.request.access
        term = self.get_term()
        week = self.get_week(term)
        location = self.get_location()
        params = self.request.GET
        full_view = access.is_supervisor  # leads see names and free/busy only (D25)
        employees = list(
            self.org_queryset(Employee)
            .current()
            .exclude(employment_type="inactive")
            .prefetch_related(Prefetch("employee_tags", queryset=EmployeeTag.objects.select_related("tag")))
        )
        if q := params.get("q", "").strip().lower():
            employees = [e for e in employees if q in e.display_name.lower() or q in e.full_name.lower()]
        if full_view and (tag := params.get("tag")):
            employees = [e for e in employees if any(str(et.tag_id) == tag for et in e.employee_tags.all())]
        rows = []
        summaries = week_summaries(employees, term, location, week) if location else {}
        for e in employees:
            s = summaries.get(e.pk)
            if s is None:
                continue
            if (status := params.get("status")) and s.status != status:
                continue
            rows.append(
                {"employee": e, "summary": s, "days": [day_bar(d) for d in s.days], "tags": e.tags_on(week)}
            )
        ctx.update(
            term=term,
            terms=self.org_queryset(Term),
            week=week,
            prev_week=week - timedelta(days=7),
            next_week=week + timedelta(days=7),
            days=cal.week_days(week),
            rows=rows,
            coverage=coverage_summary([r["summary"] for r in rows]),
            location=location,
            full_view=full_view,
            tags=self.org_queryset(Tag) if full_view else [],
            statuses=Status.choices,
            filters=params,
        )
        return ctx


def day_bar(day) -> dict:
    """Segments for one day's bar, as percentages of the day's span from first opening to
    last closing: closed gaps (dark), unavailable (hatched) and preferred (solid)."""
    if not day.open:
        return {"closed": True}
    span = Interval(day.open[0].start, day.open[-1].end)
    width = span.minutes

    def seg(iv: Interval, kind: str) -> dict:
        return {
            "kind": kind,
            "left": round((iv.start - span.start) / width * 100, 3),
            "width": round(iv.minutes / width * 100, 3),
            "label": iv.label(),
        }

    gaps = [Interval(a.end, b.start) for a, b in zip(day.open, day.open[1:], strict=False)]
    return {
        "closed": False,
        "span": span.label(),
        "segments": [seg(g, "closed") for g in gaps]
        + [seg(iv, "unavailable") for iv in intersect(day.unavailable, [span])]
        + [seg(iv, "preferred") for iv in day.preferred],
        "unavailable": [iv.label() for iv in day.unavailable],
        "preferred": [iv.label() for iv in day.preferred],
        "fully_free": day.fully_free,
        "fully_out": day.fully_out,
    }


class MyTimeOffView(OrgViewMixin, TemplateView):
    """An employee's own time-off requests (SPEC §9.2)."""

    template_name = "availability/my_time_off.html"

    def employee(self) -> Employee:
        employee = self.request.access.employee
        if employee is None:
            raise Http404("Your account isn't linked to an employee record.")
        return employee

    def get_context_data(self, form=None, **kwargs):
        ctx = super().get_context_data(**kwargs)
        employee = self.employee()
        ctx["form"] = form or TimeOffForm(today=timezone.localdate())
        ctx["requests"] = TimeOff.objects.filter(employee=employee).order_by("-start_date")[:30]
        ctx["today"] = timezone.localdate()
        return ctx

    def post(self, request, *args, **kwargs):
        employee = self.employee()
        if request.POST.get("action") == "cancel":
            t = get_object_or_404(TimeOff.objects.filter(employee=employee), pk=request.POST.get("request"))
            try:
                time_off.cancel(t)
            except time_off.TimeOffError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, f"Cancelled your request for {t.label()}.")
            return redirect("my_time_off")
        form = TimeOffForm(request.POST, today=timezone.localdate())
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))
        d = form.cleaned_data
        t = time_off.request(employee, d["start_date"], d["end_date"], d["start_min"], d["end_min"])
        messages.success(request, f"Requested time off for {t.label()}. A supervisor will review it.")
        return redirect("my_time_off")


class TimeOffReviewView(OrgViewMixin, TemplateView):
    """Pending requests with their coverage impact, then approve or deny (SPEC §9.2)."""

    permission = Perm.EDIT_SCHEDULE
    template_name = "availability/time_off_review.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        org = self.request.organization
        pending = TimeOff.objects.filter(organization=org, status=TimeOffStatus.PENDING).select_related(
            "employee"
        )
        ctx["pending"] = [(t, time_off.clashes(t)) for t in pending]
        ctx["recent"] = (
            TimeOff.objects.filter(organization=org)
            .exclude(status=TimeOffStatus.PENDING)
            .select_related("employee", "decided_by")
            .order_by("-decided_at", "-created_at")[:20]
        )
        return ctx

    def post(self, request, *args, **kwargs):
        t = get_object_or_404(
            TimeOff.objects.filter(organization=request.organization), pk=request.POST.get("request")
        )
        approve = request.POST.get("action") == "approve"
        try:
            time_off.decide(t, request.user, approve)
        except time_off.TimeOffError as exc:
            messages.error(request, str(exc))
            return redirect("time_off_review")
        if not approve:
            messages.success(request, f"Denied {t.employee.display_name}'s time off for {t.label()}.")
            return redirect("time_off_review")
        n = len(time_off.clashes(t))
        extra = (
            f" {n} of their shifts now clash{'es' if n == 1 else ''} with it and show as availability "
            "conflicts until reassigned."
            if n
            else ""
        )
        messages.success(request, f"Approved {t.employee.display_name}'s time off for {t.label()}.{extra}")
        return redirect("time_off_review")
