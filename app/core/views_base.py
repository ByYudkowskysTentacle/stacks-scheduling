# SPDX-License-Identifier: AGPL-3.0-or-later
"""Base view mixins: every page goes through `OrgViewMixin`, which signs people in,
requires a membership and checks the view's permission server-side (CLAUDE.md §6)."""

from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import Model, QuerySet

from app.core.permissions import Perm


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
        """Rows of `model` belonging to the current organization only."""
        return model._default_manager.filter(organization=self.request.organization)  # type: ignore[attr-defined]


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
