# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.generic import CreateView, DeleteView, ListView, TemplateView, UpdateView

from app.core import audit_log, departments, retention
from app.core.forms import (
    ClosureForm,
    DepartmentForm,
    DepartmentRemoveForm,
    LocationForm,
    OperatingHoursForm,
    OrganizationForm,
    RetentionPolicyForm,
    TermForm,
)
from app.core.models import (
    Closure,
    Department,
    Location,
    Membership,
    OperatingHoursSet,
    Role,
    SupervisorDepartment,
    Term,
)
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
        if (employee := self.request.access.employee) is not None:
            from app.availability.models import Availability
            from app.scheduling.published import my_upcoming

            ctx["upcoming"] = my_upcoming(employee, timezone.localdate(), weeks=2)[:3]

            term = Term.current_for(org, timezone.localdate())
            if term is not None:
                availability = Availability.objects.filter(employee=employee, term=term).first()
                ctx["term"] = term
                ctx["availability_status"] = (
                    availability.get_status_display() if availability else "Not entered"
                )
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


class DepartmentListView(OrgViewMixin, ListView):
    """Departments plan their own schedules (D95)."""

    permission = Perm.CONFIGURE
    template_name = "core/departments.html"

    def get_queryset(self):
        return self.org_queryset(Department).prefetch_related("supervisors__user")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["can_remove"] = self.request.access.has(Perm.MANAGE_MEMBERS)
        return ctx


class DepartmentFormMixin(OrgObjectMixin):
    permission = Perm.CONFIGURE
    model = Department
    form_class = DepartmentForm
    template_name = "core/form.html"
    success_url = reverse_lazy("departments")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()  # type: ignore[misc]
        kwargs["organization"] = self.request.organization
        return kwargs


class DepartmentCreateView(DepartmentFormMixin, CreateView):
    extra_context = {"title": "Add a department"}


class DepartmentUpdateView(DepartmentFormMixin, UpdateView):
    extra_context = {"title": "Edit department"}


class DepartmentRemoveView(OrgViewMixin, TemplateView):
    """Remove a department, merging what it holds into another first (D110). Admins only, since
    it changes what other supervisors manage."""

    permission = Perm.MANAGE_MEMBERS
    template_name = "core/department_remove.html"

    def get_department(self) -> Department:
        return get_object_or_404(self.org_queryset(Department), pk=self.kwargs["pk"])

    def render_form(self, department: Department, form, error: str = ""):
        found = departments.usage(department)
        return self.render_to_response(
            self.get_context_data(
                department=department,
                usage=found,
                only=not self.org_queryset(Department).exclude(pk=department.pk).exists(),
                form=form,
                error=error,
            )
        )

    def get(self, request, pk):
        department = self.get_department()
        needs = not departments.usage(department).empty
        return self.render_form(department, DepartmentRemoveForm(department=department, needs_target=needs))

    def post(self, request, pk):
        department = self.get_department()
        needs = not departments.usage(department).empty
        form = DepartmentRemoveForm(request.POST, department=department, needs_target=needs)
        if not form.is_valid():
            return self.render_form(department, form)
        into = form.cleaned_data.get("into")
        name = department.name
        try:
            showed = departments.remove(department, into)
        except departments.DepartmentError as exc:
            return self.render_form(department, form, str(exc))
        done = f"Removed {name}." + (f" Everything it held is now in {into}." if into else "")
        if showed:
            verb = "shows" if len(showed) == 1 else "show"
            done += f" {', '.join(showed)} no longer {verb} its open shifts to their people."
        messages.success(request, done)
        return redirect("departments")


class MemberListView(OrgViewMixin, TemplateView):
    """Admins grant the supervisor and admin roles. Employees get their membership by invitation."""

    permission = Perm.MANAGE_MEMBERS
    template_name = "core/members.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["members"] = (
            self.org_queryset(Membership)
            .select_related("user")
            .prefetch_related("departments")
            .order_by("role", "user__email")
        )
        ctx["roles"] = Role.choices
        ctx["departments"] = self.org_queryset(Department)
        return ctx

    def post(self, request, *args, **kwargs):
        member = get_object_or_404(self.org_queryset(Membership), pk=request.POST.get("membership"))
        if request.POST.get("action") == "departments":
            return self.set_departments(request, member)
        role = request.POST.get("role")
        if role not in Role.values:
            messages.error(request, "Unknown role.")
        elif member.user_id == request.user.pk and role != Role.ADMIN:
            messages.error(request, "You can't remove your own admin role. Ask another admin.")
        else:
            member.role = role
            member.save(update_fields=["role"])
            departments = list(self.org_queryset(Department)[:2])
            if role == Role.SUPERVISOR and len(departments) == 1:
                SupervisorDepartment.objects.get_or_create(membership=member, department=departments[0])
            messages.success(request, f"{member.user.email} is now {member.get_role_display().lower()}.")
        return redirect("members")

    def set_departments(self, request, member: Membership):
        """Which departments a supervisor manages (D95). Admins always manage all of them."""
        wanted = set(self.org_queryset(Department).filter(pk__in=request.POST.getlist("department")))
        links = {link.department: link for link in member.department_links.select_related("department")}
        for dept, link in links.items():
            if dept not in wanted:
                link.delete()
        for dept in wanted - set(links):
            SupervisorDepartment.objects.create(membership=member, department=dept)
        names = ", ".join(sorted(d.name for d in wanted)) or "no departments"
        messages.success(request, f"{member.user.email} manages {names}.")
        return redirect("members")


class AuditLogView(OrgViewMixin, TemplateView):
    permission = Perm.VIEW_AUDIT
    template_name = "core/audit_log.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        page = Paginator(audit_log.events_for(self.request.organization, self.request.access), 50).get_page(
            self.request.GET.get("page")
        )
        ctx["page"] = page
        ctx["rows"] = audit_log.describe(page.object_list)
        return ctx


class RetentionView(OrgViewMixin, TemplateView):
    """Retention periods per data type (SPEC §11), what they would purge today, and a
    confirmed "purge now". Administrators only: a purge can't be undone."""

    permission = Perm.MANAGE_MEMBERS
    template_name = "core/retention.html"

    def get_context_data(self, form=None, **kwargs):
        ctx = super().get_context_data(**kwargs)
        policy = retention.policy_for(self.request.organization)
        ctx.update(
            form=form or RetentionPolicyForm(instance=policy),
            policy=policy,
            due=retention.preview(self.request.organization),
        )
        return ctx

    def post(self, request, *args, **kwargs):
        org = request.organization
        policy = retention.policy_for(org)
        if request.POST.get("action") == "suggest":
            # Fill the form only: nothing is saved until the admin presses Save (D105).
            form = RetentionPolicyForm(instance=policy, initial=retention.SUGGESTED)
            messages.info(request, "Suggested periods filled in below. Review them, then save.")
            return self.render_to_response(self.get_context_data(form=form))
        if request.POST.get("action") == "purge":
            if request.POST.get("confirm") != "yes":
                messages.error(request, "Tick the confirmation box to purge.")
                return redirect("retention")
            counts = retention.purge(org)
            summary = ", ".join(f"{label}: {n}" for label, n in counts.items()) or "no periods are set"
            messages.success(request, f"Purged expired data ({summary}).")
            return redirect("retention")
        form = RetentionPolicyForm(request.POST, instance=policy)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))
        form.save()
        messages.success(
            request, "Retention periods saved. Nothing is purged until the nightly run or Purge now."
        )
        return redirect("retention")
