# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from app.core.permissions import NO_ACCESS, resolve_access

SESSION_KEY = "organization_id"


class OrganizationMiddleware:
    """Attach the current organization and the user's `Access` to every request.

    Most installations have one organization. When a user belongs to several, the
    choice is kept in the session and only ever resolved from their own memberships.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.access = NO_ACCESS
        request.organization = None
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            memberships = list(user.memberships.select_related("organization").order_by("organization_id"))
            chosen = next(
                (m for m in memberships if m.organization_id == request.session.get(SESSION_KEY)),
                memberships[0] if memberships else None,
            )
            if chosen is not None:
                employee = user.employee_records.filter(
                    organization=chosen.organization, archived_at__isnull=True
                ).first()
                request.access = resolve_access(chosen, employee)
                request.organization = chosen.organization
        return self.get_response(request)
