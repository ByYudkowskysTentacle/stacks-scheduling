# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from django.contrib import messages
from django.db import transaction
from django.db.models import Prefetch, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views import View
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from app.core.permissions import Perm
from app.core.views_base import OrgObjectMixin, OrgViewMixin
from app.people import invitations
from app.people.forms import (
    EmployeeForm,
    PayRoleForm,
    TagForm,
    employee_pay_role_formset,
    employee_tag_formset,
)
from app.people.models import Employee, EmployeeTag, EmploymentType, PayRole, Tag


class EmployeeListView(OrgViewMixin, ListView):
    permission = Perm.VIEW_ROSTER
    template_name = "people/employee_list.html"

    def get_queryset(self):
        qs = (
            self.org_queryset(Employee)
            .select_related("default_pay_role", "user")
            .prefetch_related(Prefetch("employee_tags", queryset=EmployeeTag.objects.select_related("tag")))
        )
        params = self.request.GET
        qs = qs.filter(archived_at__isnull=False) if params.get("archived") == "1" else qs.current()
        if q := params.get("q", "").strip():
            qs = qs.filter(Q(full_name__icontains=q) | Q(preferred_name__icontains=q))
        if tag := params.get("tag"):
            qs = qs.filter(employee_tags__tag_id=tag).distinct()
        if etype := params.get("type"):
            qs = qs.filter(employment_type=etype)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        today = timezone.localdate()
        ctx["rows"] = [(e, e.tags_on(today)) for e in ctx["object_list"]]
        ctx["tags"] = self.org_queryset(Tag)
        ctx["types"] = EmploymentType.choices
        ctx["filters"] = self.request.GET
        return ctx


class EmployeeDetailView(OrgObjectMixin, DetailView):
    permission = Perm.VIEW_ROSTER
    model = Employee
    template_name = "people/employee_detail.html"

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("default_pay_role", "user")
            .prefetch_related("pay_roles__pay_role", "employee_tags__tag")
        )


class EmployeeEditMixin(OrgObjectMixin):
    permission = Perm.MANAGE_PEOPLE
    model = Employee
    form_class = EmployeeForm
    template_name = "people/employee_form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()  # type: ignore[misc]
        access = self.request.access
        kwargs.update(
            organization=self.request.organization,
            can_view_pay=access.has(Perm.VIEW_PAY),
            can_view_notes=access.has(Perm.VIEW_NOTES),
        )
        return kwargs

    def formsets(self, instance):
        org = self.request.organization
        data = self.request.POST if self.request.method == "POST" else None
        sets = {"tag_formset": employee_tag_formset(org, data=data, instance=instance)}
        if self.request.access.has(Perm.VIEW_PAY):
            sets["pay_formset"] = employee_pay_role_formset(org, data=data, instance=instance)
        return sets

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)  # type: ignore[misc]
        if "tag_formset" not in kwargs:
            ctx.update(self.formsets(self.object if getattr(self, "object", None) else Employee()))
        return ctx

    def post(self, request, *args, **kwargs):
        self.object = self.get_object() if "pk" in kwargs else None
        form = self.get_form()
        sets = self.formsets(self.object or Employee(organization=request.organization))
        if form.is_valid() and all(fs.is_valid() for fs in sets.values()):
            with transaction.atomic():
                form.instance.organization = request.organization
                self.object = form.save()
                for fs in sets.values():
                    fs.instance = self.object
                    for f in fs.forms:
                        f.instance.organization = request.organization
                    fs.save()
            messages.success(request, f"Saved {self.object.display_name}.")
            return redirect("employee_detail", pk=self.object.pk)
        return self.render_to_response(self.get_context_data(form=form, **sets))


class EmployeeCreateView(EmployeeEditMixin, CreateView):
    extra_context = {"title": "Add an employee"}


class EmployeeUpdateView(EmployeeEditMixin, UpdateView):
    extra_context = {"title": "Edit employee"}

    def get_queryset(self):
        return super().get_queryset().current()


class EmployeeArchiveView(OrgViewMixin, View):
    """Archiving is a confirmed, audited action, never a delete (CLAUDE.md §2.3)."""

    permission = Perm.MANAGE_PEOPLE

    def get_employee(self, pk) -> Employee:
        return get_object_or_404(self.org_queryset(Employee), pk=pk)

    def get(self, request, pk):
        employee = self.get_employee(pk)
        return render(request, "people/employee_archive.html", {"employee": employee})

    def post(self, request, pk):
        employee = self.get_employee(pk)
        if request.POST.get("action") == "restore":
            employee.archived_at = None
            employee.save(update_fields=["archived_at"])
            messages.success(request, f"Restored {employee.display_name}.")
        elif request.POST.get("confirm") == "yes":
            employee.archived_at = timezone.now()
            employee.in_scheduling_pool = False
            employee.save(update_fields=["archived_at", "in_scheduling_pool"])
            messages.success(
                request, f"Archived {employee.display_name}. Their records are kept and can be restored."
            )
        else:
            messages.error(request, "Tick the confirmation box to archive.")
            return redirect("employee_archive", pk=pk)
        return redirect("employee_detail", pk=pk)


class EmployeeInviteView(OrgViewMixin, View):
    """Create (or link) a sign-in account for an employee and email them a link to set a password."""

    permission = Perm.MANAGE_PEOPLE

    def post(self, request, pk):
        employee = get_object_or_404(self.org_queryset(Employee).current(), pk=pk)
        try:
            invitations.invite(request, employee)
        except invitations.InviteError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"Invitation sent to {employee.display_name}.")
        return redirect("employee_detail", pk=pk)


class PayRoleListView(OrgViewMixin, ListView):
    permission = Perm.CONFIGURE
    template_name = "people/pay_roles.html"

    def get_queryset(self):
        return self.org_queryset(PayRole)


class PayRoleCreateView(OrgObjectMixin, CreateView):
    permission = Perm.CONFIGURE
    model = PayRole
    form_class = PayRoleForm
    template_name = "core/form.html"
    success_url = reverse_lazy("pay_roles")
    extra_context = {"title": "Add a pay role"}


class PayRoleUpdateView(OrgObjectMixin, UpdateView):
    permission = Perm.CONFIGURE
    model = PayRole
    form_class = PayRoleForm
    template_name = "core/form.html"
    success_url = reverse_lazy("pay_roles")
    extra_context = {"title": "Edit pay role"}


class TagListView(OrgViewMixin, ListView):
    permission = Perm.CONFIGURE
    template_name = "people/tags.html"

    def get_queryset(self):
        return self.org_queryset(Tag)


class TagCreateView(OrgObjectMixin, CreateView):
    permission = Perm.CONFIGURE
    model = Tag
    form_class = TagForm
    template_name = "core/form.html"
    success_url = reverse_lazy("tags")
    extra_context = {"title": "Add a tag"}


class TagUpdateView(OrgObjectMixin, UpdateView):
    permission = Perm.CONFIGURE
    model = Tag
    form_class = TagForm
    template_name = "core/form.html"
    success_url = reverse_lazy("tags")
    extra_context = {"title": "Edit tag"}

    def get_success_url(self):
        return reverse("tags")
