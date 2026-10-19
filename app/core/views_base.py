# SPDX-License-Identifier: AGPL-3.0-or-later
"""Base view mixins: every page goes through `OrgViewMixin`, which signs people in,
requires a membership and checks the view's permission server-side (CLAUDE.md §6)."""

from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import Model, QuerySet

from app.core.permissions import Access, Perm

# How each department-scoped model reaches its department (D95). A supervisor limited to some
# departments sees only the rows these paths lead into; models not listed are organization-wide
# (settings, rules, imports, the audit log).
DEPARTMENT_PATHS = {
    "people.employee": "departments",
    "scheduling.scheduleperiod": "department",
    "scheduling.shift": "period__department",
    "scheduling.callout": "shift__period__department",
    "scheduling.shiftexchange": "shift__period__department",
    "scheduling.desiredhours": "employee__departments",
    "availability.timeoff": "employee__departments",
    "availability.availability": "employee__departments",
}


def in_departments[M: Model](queryset: QuerySet[M], access: Access) -> QuerySet[M]:
    """`queryset` limited to the departments `access` may see. Server-side, on every view that
    lists or fetches people, schedules, or the records hanging off them."""
    if access.department_ids is None:
        return queryset
    path = DEPARTMENT_PATHS.get(queryset.model._meta.label_lower)
    if path is None:
        return queryset
    scoped = queryset.filter(**{f"{path}__in": access.department_ids})
    return scoped.distinct() if "departments" in path else scoped


class OrgViewMixin(LoginRequiredMixin):
    permission: Perm | None = None

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if request.organization is None:
            raise PermissionDenied("Your account isn't linked to an organization yet. Ask a supervisor.")
        if self.permission is not None and not request.access.has(self.permission):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def org_queryset[M: Model](self, model: type[M]) -> QuerySet[M]:
        """Rows of `model` belonging to the current organization, and to the departments the
        user may see (D95)."""
        rows = model._default_manager.filter(organization=self.request.organization)  # type: ignore[attr-defined]
        return in_departments(rows, self.request.access)


class OrgObjectMixin(OrgViewMixin):
    """For generic views: scope `get_queryset` to the organization and stamp it on save."""

    model: type[Model]

    def get_queryset(self):
        return self.org_queryset(self.model)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)  # type: ignore[misc]
        success_url = getattr(self, "success_url", None)
        if success_url:
            ctx.setdefault("cancel_url", str(success_url))
        return ctx

    def form_valid(self, form):
        if hasattr(form.instance, "organization_id") and not form.instance.organization_id:
            form.instance.organization = self.request.organization
        return super().form_valid(form)  # type: ignore[misc]
