# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from app.core.models import Location
from app.core.permissions import Perm
from app.core.views_base import OrgViewMixin
from app.people.models import Employee
from app.scheduling import services
from app.scheduling.forms import (
    CallOutForm,
    CoverageOverrideForm,
    DutyForm,
    HourCapForm,
    HourTargetForm,
    PeriodForm,
    RotationForm,
    SchedulingRulesForm,
    ShiftForm,
    TagRuleForm,
)
from app.scheduling.models import (
    CallOut,
    CoverageOverride,
    DutyAssignment,
    HourCapRule,
    HourTargetRule,
    Notification,
    RotationAssignment,
    SchedulePeriod,
    SchedulingRules,
    Shift,
    TagCoverageRule,
)
from domain.calendar import closures_on, scheduled_intervals
from domain.schedule import on_duty_by_slot, slots_of
from domain.timeutil import SLOT_MINUTES, format_minute_12h
from domain.validation import CATEGORY_LABELS, Report, Severity


def report_context(report: Report) -> dict:
    groups = []
    for category, issues in sorted(
        report.by_category().items(), key=lambda kv: (min(i.severity != Severity.ERROR for i in kv[1]), kv[0])
    ):
        singular, plural = CATEGORY_LABELS[category]
        groups.append(
            {"label": plural if len(issues) != 1 else singular, "issues": issues, "category": category.value}
        )
    return {"report": report, "report_groups": groups}


def flash_validation(request, period: SchedulePeriod, what: str) -> None:
    """After every schedule change, run the full suite and say what it found (CLAUDE.md §2.4)."""
    report = services.validate_period(period)
    level = messages.SUCCESS if not report.counted else messages.WARNING
    messages.add_message(request, level, f"{what} Validation: {report.summary()}.")


class PeriodMixin(OrgViewMixin):
    def get_period(self) -> SchedulePeriod:
        return get_object_or_404(
            self.org_queryset(SchedulePeriod).select_related("location", "organization"),
            pk=self.kwargs["period_pk"],
        )


class ScheduleIndexView(OrgViewMixin, TemplateView):
    permission = Perm.VIEW_DRAFTS
    template_name = "scheduling/index.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        periods = list(self.org_queryset(SchedulePeriod).select_related("location"))
        rows = []
        for p in periods:
            latest = p.latest_version()
            pending = services.pending_changes(p)
            rows.append({"period": p, "latest": latest, "pending_people": len(pending)})
        ctx["rows"] = rows
        ctx.setdefault("form", PeriodForm(organization=self.request.organization))
        ctx["can_create"] = self.request.access.has(Perm.EDIT_SCHEDULE)
        return ctx

    def post(self, request, *args, **kwargs):
        if not request.access.has(Perm.EDIT_SCHEDULE):
            raise PermissionDenied
        form = PeriodForm(request.POST, organization=request.organization)
        location = self.org_queryset(Location).filter(is_active=True).first()
        if location is None:
            messages.error(request, "Add a location first.")
            return redirect("schedule_index")
        if form.is_valid():
            existing = SchedulePeriod.objects.filter(
                location=location, start_date=form.cleaned_data["start_date"]
            ).first()
            if existing:
                messages.info(request, "That period already exists.")
                return redirect("period", period_pk=existing.pk)
            period = form.save(commit=False)
            period.organization = request.organization
            period.location = location
            period.save()
            return redirect("period", period_pk=period.pk)
        return self.render_to_response(self.get_context_data(form=form))


class PeriodView(PeriodMixin, TemplateView):
    """The schedule grid by person (SPEC §14): rows are people, columns are days of one week,
    with hours against limits and the validation report."""

    permission = Perm.VIEW_DRAFTS
    template_name = "scheduling/period.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        period = self.get_period()
        access = self.request.access
        try:
            week = max(0, min(period.weeks - 1, int(self.request.GET.get("week", 0))))
        except ValueError:
            week = 0
        days = period.days[week * 7 : (week + 1) * 7]
        shifts = list(period.shifts.live().select_related("employee", "pay_role"))
        dctx = services.build_context(period, shifts)
        report = services.validate(dctx)
        states = services.shift_states(period)
        employees = list(services.employees_for_grid(period))
        by_cell: dict[tuple[int | None, date], list[Shift]] = defaultdict(list)
        for s in shifts:
            by_cell[(s.employee_id, s.date)].append(s)
        hours = {r.employee.pk: r for r in services.hours_rows(period, employees, dctx)}
        rows = []
        for e in sorted(employees, key=lambda e: e.display_name.lower()):
            person = dctx.people.get(e.pk)
            rows.append(
                {
                    "employee": e,
                    "cells": [by_cell.get((e.pk, d), []) for d in days],
                    "hours": hours.get(e.pk),
                    "week_minutes": hours[e.pk].weeks[week] if e.pk in hours else (0, None),
                    "tags": sorted(person.tags_by_day.get(days[0], ()))
                    if person and access.is_supervisor
                    else [],
                    "status": person.availability.status if person else "not_entered",
                }
            )
        open_shifts = [[s for s in by_cell.get((None, d), [])] for d in days]
        duty = on_duty_by_slot(dctx.shifts)
        coverage = []
        for d in days:
            gaps = sum(i.minutes for i in report.issues if i.day == d and i.category.value == "coverage_gap")
            over = sum(i.minutes for i in report.issues if i.day == d and i.category.value == "over_coverage")
            open_ivs = dctx.open_on(d)
            people_hours = sum(len(duty[d][slot]) for iv in open_ivs for slot in slots_of(iv)) * SLOT_MINUTES
            coverage.append(
                {"day": d, "gap": gaps, "over": over, "scheduled": people_hours, "open": open_ivs}
            )
        ctx.update(
            period=period,
            week=week,
            weeks=range(period.weeks),
            days=days,
            rows=rows,
            open_shifts=open_shifts,
            coverage=coverage,
            states=states,
            latest=period.latest_version(),
            pending=services.pending_changes(period),
            removed=period.shifts.filter(deleted_at__isnull=False).select_related("employee")[:20],
            **report_context(report),
        )
        return ctx


class DayView(PeriodMixin, TemplateView):
    """The schedule by time for one day: coverage per 15-minute slot, with closures shown."""

    permission = Perm.VIEW_DRAFTS
    template_name = "scheduling/day.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        period = self.get_period()
        try:
            day = date.fromisoformat(self.kwargs["day"])
        except ValueError as exc:
            raise Http404 from exc
        if not period.contains(day):
            raise Http404
        shifts = list(period.shifts.live().filter(date=day).select_related("employee"))
        dctx = services.build_context(period, list(period.shifts.live()))
        cov = dctx.rules.coverage
        duty = on_duty_by_slot(s for s in dctx.shifts if s.day == day)[day]
        open_ivs = dctx.open_on(day)
        scheduled = scheduled_intervals(day, dctx.hours)
        closures = closures_on(day, dctx.closures)
        names = {s.employee_id: s.employee.display_name for s in shifts if s.employee}
        span_start = min((iv.start for iv in scheduled), default=0)
        span_end = max((iv.end for iv in scheduled), default=0)
        rows = []
        for slot in range(span_start // SLOT_MINUTES, span_end // SLOT_MINUTES):
            minute = slot * SLOT_MINUTES
            is_open = any(iv.start <= minute < iv.end for iv in open_ivs)
            on = duty.get(slot, [])
            target = cov.target(day.weekday(), minute) if is_open else 0
            closure = next((c.reason for c in closures if c.span.start <= minute < c.span.end), "")
            rows.append(
                {
                    "time": format_minute_12h(minute),
                    "open": is_open,
                    "closure": closure,
                    "target": target,
                    "count": len(on),
                    "names": sorted(names.get(e, "?") for e in on),
                    "state": slot_state(is_open, len(on), target, cov.mode),
                }
            )
        ctx.update(
            period=period,
            day=day,
            rows=rows,
            shifts=shifts,
            prev_day=day - timedelta(days=1),
            next_day=day + timedelta(days=1),
        )
        return ctx


def slot_state(is_open: bool, count: int, target: int, mode: str) -> str:
    if not is_open:
        return "closed"
    if count < target:
        return "short"
    if count > target and mode == "exact":
        return "over"
    return "ok"


class ShiftEditMixin(PeriodMixin):
    """Supervisors edit drafts; shift leads propose them (D25). Either way nothing is
    published or sent until a supervisor publishes."""

    template_name = "scheduling/shift_form.html"

    def check_access(self):
        access = self.request.access
        if not (access.has(Perm.EDIT_SCHEDULE) or access.has(Perm.PROPOSE_EDITS)):
            raise PermissionDenied

    def get_shift(self, period) -> Shift | None:
        return None

    def get(self, request, *args, **kwargs):
        self.check_access()
        period = self.get_period()
        shift = self.get_shift(period)
        initial = {}
        if shift is None:
            initial = {k: request.GET.get(k) for k in ("employee", "date") if request.GET.get(k)}
        form = ShiftForm(instance=shift, period=period, initial=initial)
        return render(request, self.template_name, {"form": form, "period": period, "shift": shift})

    def post(self, request, *args, **kwargs):
        self.check_access()
        period = self.get_period()
        shift = self.get_shift(period)
        form = ShiftForm(request.POST, instance=shift, period=period)
        if not form.is_valid():
            return render(request, self.template_name, {"form": form, "period": period, "shift": shift})
        saved = form.save(commit=False)
        saved.updated_by = request.user
        saved.proposed_by_lead = not request.access.has(Perm.EDIT_SCHEDULE)
        saved.save()
        what = (
            "Shift saved as a draft."
            if request.access.has(Perm.EDIT_SCHEDULE)
            else "Proposed change saved as a draft for a supervisor to review."
        )
        flash_validation(request, period, what)
        week = period.days.index(saved.date) // 7
        return redirect(reverse("period", args=[period.pk]) + f"?week={week}#validation")


class ShiftCreateView(ShiftEditMixin, View):
    pass


class ShiftUpdateView(ShiftEditMixin, View):
    def get_shift(self, period) -> Shift:
        return get_object_or_404(period.shifts.live(), pk=self.kwargs["pk"])


class ShiftDeleteView(PeriodMixin, View):
    """Removing a shift needs confirmation and is restorable (CLAUDE.md §2.3)."""

    permission = Perm.EDIT_SCHEDULE

    def get(self, request, period_pk, pk):
        period = self.get_period()
        shift = get_object_or_404(period.shifts.live(), pk=pk)
        return render(request, "scheduling/shift_delete.html", {"period": period, "shift": shift})

    def post(self, request, period_pk, pk):
        period = self.get_period()
        shift = get_object_or_404(period.shifts.all(), pk=pk)
        if request.POST.get("action") == "restore":
            shift.deleted_at = None
            shift.updated_by = request.user
            shift.save()
            flash_validation(request, period, f"Restored {shift}.")
        elif request.POST.get("confirm") == "yes" and shift.deleted_at is None:
            shift.deleted_at = timezone.now()
            shift.updated_by = request.user
            shift.save()
            flash_validation(
                request, period, f"Removed {shift} from the draft. You can restore it from the period page."
            )
        else:
            messages.error(request, "Tick the confirmation box to remove the shift.")
            return redirect("shift_delete", period_pk=period.pk, pk=pk)
        return redirect(reverse("period", args=[period.pk]) + f"?week={period.days.index(shift.date) // 7}")


class CallOutView(PeriodMixin, View):
    """Record a day-of call-out and who covered (D25). Changes the draft and goes to review."""

    def check_access(self):
        access = self.request.access
        if not (access.has(Perm.EDIT_SCHEDULE) or access.has(Perm.RECORD_CALL_OUTS)):
            raise PermissionDenied

    def get(self, request, period_pk, pk):
        self.check_access()
        period = self.get_period()
        shift = get_object_or_404(period.shifts.live().exclude(employee=None), pk=pk)
        return render(
            request,
            "scheduling/call_out.html",
            {"period": period, "shift": shift, "form": CallOutForm(shift=shift)},
        )

    def post(self, request, period_pk, pk):
        self.check_access()
        period = self.get_period()
        shift = get_object_or_404(period.shifts.live().exclude(employee=None), pk=pk)
        form = CallOutForm(request.POST, shift=shift)
        if not form.is_valid():
            return render(
                request, "scheduling/call_out.html", {"period": period, "shift": shift, "form": form}
            )
        cover = form.cleaned_data["covered_by"]
        with transaction.atomic():
            CallOut.objects.create(
                organization=period.organization,
                shift=shift,
                called_out=shift.employee,
                covered_by=cover,
                note=form.cleaned_data["note"],
                recorded_by=request.user,
            )
            shift.employee = cover
            if cover is not None and cover.default_pay_role_id:
                shift.pay_role = cover.default_pay_role
            shift.updated_by = request.user
            shift.proposed_by_lead = not request.access.has(Perm.EDIT_SCHEDULE)
            shift.save()
        flash_validation(
            request, period, "Call-out recorded for supervisor review. No one has been notified."
        )
        return redirect(reverse("period", args=[period.pk]) + f"?week={period.days.index(shift.date) // 7}")


class ReviewQueueView(OrgViewMixin, TemplateView):
    """Shift leads' draft changes and call-outs, for a supervisor to keep or revert (scenario 17)."""

    permission = Perm.EDIT_SCHEDULE
    template_name = "scheduling/review.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        org = self.request.organization
        ctx["proposals"] = (
            Shift.objects.filter(organization=org, proposed_by_lead=True)
            .select_related("employee", "period", "updated_by")
            .order_by("date", "start_min")
        )
        ctx["call_outs"] = CallOut.objects.filter(organization=org, reviewed_at__isnull=True).select_related(
            "shift__period", "called_out", "covered_by", "recorded_by"
        )
        return ctx

    def post(self, request, *args, **kwargs):
        org = request.organization
        action = request.POST.get("action")
        if action in ("keep", "revert"):
            shift = get_object_or_404(
                Shift.objects.filter(organization=org, proposed_by_lead=True), pk=request.POST.get("shift")
            )
            if action == "keep":
                shift.proposed_by_lead = False
                shift.save(update_fields=["proposed_by_lead", "updated_at"])
                messages.success(request, f"Kept {shift} in the draft.")
            else:
                services.revert_shift(shift, request.user)
                flash_validation(request, shift.period, f"Reverted {shift}.")
        elif action == "reviewed":
            call_out = get_object_or_404(
                CallOut.objects.filter(organization=org), pk=request.POST.get("call_out")
            )
            call_out.reviewed_by = request.user
            call_out.reviewed_at = timezone.now()
            call_out.save()
            messages.success(request, "Call-out marked as reviewed.")
        return redirect("review_queue")


class PublishView(PeriodMixin, TemplateView):
    permission = Perm.PUBLISH
    template_name = "scheduling/publish.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        period = self.get_period()
        pending = services.pending_changes(period)
        names = {e.pk: e for e in Employee.objects.filter(pk__in=pending.keys()).select_related("user")}
        ctx.update(
            period=period,
            changes=[
                (names[k], v) for k, v in sorted(pending.items(), key=lambda kv: names[kv[0]].display_name)
            ],
            uninvited=[names[k] for k in pending if names[k].user_id is None],
            latest=period.latest_version(),
            **report_context(services.validate_period(period)),
        )
        return ctx

    def post(self, request, *args, **kwargs):
        period = self.get_period()
        if request.POST.get("confirm") != "yes":
            messages.error(request, "Tick the confirmation box to publish.")
            return redirect("publish", period_pk=period.pk)
        try:
            version = services.publish(
                period, request.user, site_url=request.build_absolute_uri("/").rstrip("/")
            )
        except services.PublishError as exc:
            messages.error(request, str(exc))
            return redirect("publish", period_pk=period.pk)
        messages.success(
            request,
            f"Published version {version.number}. "
            f"{len(version.changes)} employee(s) had changes and were notified.",
        )
        return redirect("period", period_pk=period.pk)


class VersionListView(PeriodMixin, TemplateView):
    permission = Perm.VIEW_DRAFTS
    template_name = "scheduling/versions.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        period = self.get_period()
        ctx.update(period=period, versions=period.versions.select_related("published_by"))
        return ctx


class VersionDetailView(PeriodMixin, TemplateView):
    permission = Perm.VIEW_DRAFTS
    template_name = "scheduling/version_detail.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        period = self.get_period()
        version = get_object_or_404(period.versions, number=self.kwargs["number"])
        names = dict(
            Employee.objects.filter(organization=period.organization).values_list("pk", "preferred_name")
        )
        shifts = sorted(
            (services.snapshot_to_domain(r) for r in version.shifts),
            key=lambda s: (s.day, s.interval, names.get(s.employee_id, "")),
        )
        change_names = {int(k): v for k, v in version.changes.items()}
        ctx.update(
            period=period,
            version=version,
            shifts=[(s, names.get(s.employee_id, "Open shift")) for s in shifts],
            changes=[(names.get(k, "?"), v) for k, v in change_names.items()],
        )
        return ctx


class DiscardDraftsView(PeriodMixin, View):
    permission = Perm.EDIT_SCHEDULE

    def get(self, request, period_pk):
        period = self.get_period()
        return render(
            request,
            "scheduling/discard.html",
            {
                "period": period,
                "latest": period.latest_version(),
                "pending": services.pending_changes(period),
            },
        )

    def post(self, request, period_pk):
        period = self.get_period()
        if request.POST.get("confirm") != "yes":
            messages.error(request, "Tick the confirmation box to discard the drafts.")
            return redirect("discard_drafts", period_pk=period.pk)
        try:
            touched = services.discard_drafts(period)
        except services.PublishError as exc:
            messages.error(request, str(exc))
            return redirect("period", period_pk=period.pk)
        flash_validation(request, period, f"Discarded draft changes to {touched} shift(s).")
        return redirect("period", period_pk=period.pk)


class RulesView(OrgViewMixin, TemplateView):
    """Scheduling rules: shape, coverage, caps and tag rules (SPEC §6), all configuration."""

    permission = Perm.CONFIGURE
    template_name = "scheduling/rules.html"
    FORMS = ("rules", "override", "cap", "tag_rule", "rotation", "duty", "target")
    DELETABLE = {
        "override": CoverageOverride,
        "cap": HourCapRule,
        "tag_rule": TagCoverageRule,
        "rotation": RotationAssignment,
        "duty": DutyAssignment,
        "target": HourTargetRule,
    }

    def forms(self, data=None, which=None):
        org = self.request.organization
        ws = org.week_start

        def bound(name):
            return data if which == name else None

        return {
            "rules_form": SchedulingRulesForm(bound("rules"), instance=SchedulingRules.for_org(org)),
            "override_form": CoverageOverrideForm(bound("override"), week_start=ws),
            "cap_form": HourCapForm(bound("cap"), organization=org),
            "tag_rule_form": TagRuleForm(bound("tag_rule"), organization=org, week_start=ws),
            "rotation_form": RotationForm(bound("rotation"), organization=org),
            "duty_form": DutyForm(bound("duty"), organization=org, week_start=ws),
            "target_form": HourTargetForm(bound("target"), organization=org),
        }

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        org = self.request.organization
        for k, v in self.forms().items():
            ctx.setdefault(k, v)
        ctx.update(
            overrides=CoverageOverride.objects.filter(organization=org),
            caps=HourCapRule.objects.filter(organization=org).select_related("employee", "tag"),
            tag_rules=TagCoverageRule.objects.filter(organization=org).select_related("tag"),
            rotation=RotationAssignment.objects.filter(organization=org).select_related("employee"),
            duties=DutyAssignment.objects.filter(organization=org).select_related("employee"),
            targets=HourTargetRule.objects.filter(organization=org).select_related("employee"),
        )
        return ctx

    def post(self, request, *args, **kwargs):
        org = request.organization
        which = request.POST.get("form")
        if which == "delete":
            model = self.DELETABLE.get(request.POST.get("kind", ""))
            if model is None:
                raise Http404
            obj = get_object_or_404(model.objects.filter(organization=org), pk=request.POST.get("pk"))
            if request.POST.get("confirm") != "yes":
                messages.error(request, "Tick the box next to Remove to confirm.")
            else:
                obj.delete()
                messages.success(request, f"Removed: {obj}.")
                self._revalidate_note(request)
            return redirect("rules")
        if which not in self.FORMS:
            raise Http404
        forms = self.forms(request.POST, which)
        form = forms[f"{which}_form"]
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(**forms))
        obj = form.save(commit=False)
        obj.organization = org
        if which == "rotation":
            existing = RotationAssignment.objects.filter(employee=obj.employee).first()
            if existing is not None:
                existing.group = obj.group
                obj = existing
        if which == "override":
            obj.location = self.org_queryset(Location).filter(is_active=True).first()
            if obj.location is None:
                messages.error(request, "Add a location first.")
                return redirect("rules")
        obj.save()
        messages.success(request, "Rules saved.")
        self._revalidate_note(request)
        return redirect("rules")

    def _revalidate_note(self, request) -> None:
        """D40: after a rule change, validate right away and show the new issues; change nothing."""
        today = timezone.localdate()
        period = (
            self.org_queryset(SchedulePeriod)
            .filter(start_date__lte=today + timedelta(days=28))
            .order_by("-start_date")
            .first()
        )
        if period is not None:
            report = services.validate_period(period)
            messages.info(request, f"Under the new rules, the period {period} has {report.summary()}.")


class NotificationsView(OrgViewMixin, TemplateView):
    template_name = "scheduling/notifications.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        items = list(
            Notification.objects.filter(user=self.request.user, organization=self.request.organization)[:50]
        )
        Notification.objects.filter(pk__in=[n.pk for n in items if n.read_at is None]).update(
            read_at=timezone.now()
        )
        ctx["items"] = items
        return ctx


class MyScheduleView(OrgViewMixin, TemplateView):
    """An employee's own published shifts. Drafts never show here (CLAUDE.md §2.2)."""

    template_name = "scheduling/my_schedule.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        employee = self.request.access.employee
        if employee is None:
            raise Http404("Your account isn't linked to an employee record.")
        ctx["shifts"] = services.published_shifts_for(employee, timezone.localdate())
        return ctx


class DesiredHoursSubmitView(OrgViewMixin, View):
    """An employee asks for weekly hours for a term (D32). It waits for a supervisor's approval."""

    def post(self, request):
        from app.core.models import Term
        from app.scheduling.forms import DesiredHoursForm
        from app.scheduling.models import DesiredHours, DesiredStatus

        employee = request.access.employee
        if employee is None:
            raise Http404
        term = get_object_or_404(self.org_queryset(Term), pk=request.POST.get("term"))
        form = DesiredHoursForm(request.POST)
        if form.is_valid():
            minutes = round(form.cleaned_data["hours"] * 60)
            desired, _ = DesiredHours.objects.get_or_create(
                employee=employee,
                term=term,
                defaults={"organization": request.organization, "minutes": minutes},
            )
            if desired.minutes != minutes or desired.status != DesiredStatus.PENDING:
                desired.minutes = minutes
                desired.status = DesiredStatus.PENDING
                desired.decided_by = None
                desired.decided_at = None
                desired.save()
            messages.success(request, "Thanks. A supervisor will review your desired hours.")
        else:
            messages.error(request, "Enter your desired hours as a number between 0 and 40.")
        return redirect(reverse("my_availability") + f"?term={term.pk}")


class DesiredHoursReviewView(OrgViewMixin, TemplateView):
    """Supervisors approve or decline desired hours; only approved ones reach the generator."""

    permission = Perm.EDIT_SCHEDULE
    template_name = "scheduling/desired_hours.html"

    def get_context_data(self, **kwargs):
        from app.scheduling.models import DesiredHours

        ctx = super().get_context_data(**kwargs)
        ctx["requests"] = (
            DesiredHours.objects.filter(organization=self.request.organization)
            .select_related("employee", "term", "decided_by")
            .order_by("status", "-term__start_date", "employee__preferred_name")
        )
        return ctx

    def post(self, request, *args, **kwargs):
        from app.scheduling.models import DesiredHours, DesiredStatus

        desired = get_object_or_404(
            DesiredHours.objects.filter(organization=request.organization), pk=request.POST.get("pk")
        )
        decision = request.POST.get("decision")
        if decision not in (DesiredStatus.APPROVED, DesiredStatus.DECLINED):
            raise Http404
        desired.status = decision
        desired.decided_by = request.user
        desired.decided_at = timezone.now()
        desired.save()
        messages.success(request, f"{desired.employee.display_name}: {desired.get_status_display().lower()}.")
        return redirect("desired_hours")
