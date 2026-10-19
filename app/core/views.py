# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.generic import CreateView, DeleteView, ListView, TemplateView, UpdateView

from app.core import audit_log
from app.core.forms import ClosureForm, LocationForm, OperatingHoursForm, OrganizationForm, TermForm
from app.core.models import Closure, Location, Membership, OperatingHoursSet, Role, Term
from app.core.permissions import Perm
from app.core.views_base import OrgObjectMixin, OrgViewMixin
from domain import calendar as cal
from domain.timeutil import total_minutes


class HomeView(OrgViewMixin, TemplateView):
    template_name = "core/home.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        org = self.request.organization
        if self.request.access.is_supervisor:
            ctx["setup"] = setup_checklist(org)
        return ctx


def setup_checklist(org) -> list[tuple[str, bool, str]]:
    """First-run steps for a supervisor, each with whether it's done and where to do it."""
    location = org.locations.first()
    return [
        ("Add a location", location is not None, reverse("locations")),
        ("Set operating hours", bool(location and location.hours_sets.exists()), reverse("locations")),
        ("Add a term", org.terms.exists(), reverse("terms")),
        ("Add pay roles", org.pay_roles.exists(), reverse("pay_roles")),
        ("Add employees", org.employees.exists(), reverse("employees")),
    ]


class OrganizationSettingsView(OrgViewMixin, UpdateView):
    permission = Perm.CONFIGURE
    form_class = OrganizationForm
    template_name = "core/form.html"
    success_url = reverse_lazy("organization_settings")
    extra_context = {"title": "Organization settings"}

    def get_object(self, queryset=None):
        return self.request.organization

    def form_valid(self, form):
        messages.success(self.request, "Settings saved.")
        return super().form_valid(form)


class LocationListView(OrgViewMixin, ListView):
    permission = Perm.CONFIGURE
    template_name = "core/locations.html"

    def get_queryset(self):
        return self.org_queryset(Location).prefetch_related("hours_sets__intervals", "closures")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        org = self.request.organization
        start = cal.week_start_for(timezone.localdate(), org.week_start)
        days = cal.week_days(start)
        summaries = []
        for loc in ctx["object_list"]:
            hours, closures = loc.domain_hours(), loc.domain_closures()
            week = [(d, cal.open_intervals(d, hours, closures), cal.closures_on(d, closures)) for d in days]
            summaries.append(
                {
                    "location": loc,
                    "week": week,
                    "open_minutes": total_minutes_all(week),
                }
            )
        ctx.update(summaries=summaries, week_start=start, week_end=start + timedelta(days=6))
        return ctx


def total_minutes_all(week) -> int:
    return sum(total_minutes(ivs) for _, ivs, _ in week)


class LocationCreateView(OrgObjectMixin, CreateView):
    permission = Perm.CONFIGURE
    model = Location
    form_class = LocationForm
    template_name = "core/form.html"
    success_url = reverse_lazy("locations")
    extra_context = {"title": "Add a location"}


class LocationUpdateView(OrgObjectMixin, UpdateView):
    permission = Perm.CONFIGURE
    model = Location
    form_class = LocationForm
    template_name = "core/form.html"
    success_url = reverse_lazy("locations")
    extra_context = {"title": "Edit location"}


class LocationChildMixin(OrgObjectMixin):
    """Views for rows that belong to one location (hours, closures)."""

    def get_location(self) -> Location:
        return get_object_or_404(self.org_queryset(Location), pk=self.kwargs["location_pk"])

    def get_queryset(self):
        return super().get_queryset().filter(location_id=self.kwargs["location_pk"])

    def get_success_url(self):
        return reverse("locations")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)  # type: ignore[misc]
        ctx["location"] = self.get_location()
        ctx["cancel_url"] = reverse("locations")
        return ctx


class HoursFormMixin(LocationChildMixin):
    permission = Perm.CONFIGURE
    model = OperatingHoursSet
    form_class = OperatingHoursForm
    template_name = "core/hours_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()  # type: ignore[misc]
        kwargs["week_start"] = self.request.organization.week_start
        return kwargs

    def form_valid(self, form):
        form.instance.location = self.get_location()
        messages.success(self.request, "Operating hours saved.")
        return super().form_valid(form)


class HoursCreateView(HoursFormMixin, CreateView):
    extra_context = {"title": "Add operating hours"}


class HoursUpdateView(HoursFormMixin, UpdateView):
    extra_context = {"title": "Edit operating hours"}


class HoursDeleteView(LocationChildMixin, DeleteView):
    permission = Perm.CONFIGURE
    model = OperatingHoursSet
    template_name = "core/confirm_delete.html"
    extra_context = {
        "title": "Remove operating hours",
        "consequence": "Days these hours cover will show as closed unless other hours cover them.",
    }


class ClosureFormMixin(LocationChildMixin):
    permission = Perm.CONFIGURE
    model = Closure
    form_class = ClosureForm
    template_name = "core/form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()  # type: ignore[misc]
        kwargs["location"] = self.get_location()
        return kwargs

    def form_valid(self, form):
        messages.success(self.request, "Closure saved.")
        return super().form_valid(form)


class ClosureCreateView(ClosureFormMixin, CreateView):
    extra_context = {"title": "Add a closure"}

    def get_initial(self):
        return {"kind": Closure.RECURRING}


class ClosureUpdateView(ClosureFormMixin, UpdateView):
    extra_context = {"title": "Edit closure"}


class ClosureDeleteView(LocationChildMixin, DeleteView):
    permission = Perm.CONFIGURE
    model = Closure
    template_name = "core/confirm_delete.html"
    extra_context = {
        "title": "Remove closure",
        "consequence": "The location will count as open during this time.",
    }


class TermListView(OrgViewMixin, ListView):
    permission = Perm.CONFIGURE
    template_name = "core/terms.html"

    def get_queryset(self):
        return self.org_queryset(Term)


class TermCreateView(OrgObjectMixin, CreateView):
    permission = Perm.CONFIGURE
    model = Term
    form_class = TermForm
    template_name = "core/form.html"
    success_url = reverse_lazy("terms")
    extra_context = {"title": "Add a term"}


class TermUpdateView(OrgObjectMixin, UpdateView):
    permission = Perm.CONFIGURE
    model = Term
    form_class = TermForm
    template_name = "core/form.html"
    success_url = reverse_lazy("terms")
    extra_context = {"title": "Edit term"}


class MemberListView(OrgViewMixin, TemplateView):
    """Admins grant the supervisor and admin roles. Employees get their membership by invitation."""

    permission = Perm.MANAGE_MEMBERS
    template_name = "core/members.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["members"] = self.org_queryset(Membership).select_related("user").order_by("role", "user__email")
        ctx["roles"] = Role.choices
        return ctx

    def post(self, request, *args, **kwargs):
        member = get_object_or_404(self.org_queryset(Membership), pk=request.POST.get("membership"))
        role = request.POST.get("role")
        if role not in Role.values:
            messages.error(request, "Unknown role.")
        elif member.user_id == request.user.pk and role != Role.ADMIN:
            messages.error(request, "You can't remove your own admin role. Ask another admin.")
        else:
            member.role = role
            member.save(update_fields=["role"])
            messages.success(request, f"{member.user.email} is now {member.get_role_display().lower()}.")
        return redirect("members")


class AuditLogView(OrgViewMixin, TemplateView):
    permission = Perm.VIEW_AUDIT
    template_name = "core/audit_log.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        page = Paginator(audit_log.events_for(self.request.organization), 50).get_page(
            self.request.GET.get("page")
        )
        ctx["page"] = page
        ctx["rows"] = audit_log.describe(page.object_list)
        return ctx
