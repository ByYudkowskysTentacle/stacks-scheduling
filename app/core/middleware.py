# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from django.utils import timezone

from app.core.permissions import NO_ACCESS, resolve_access
from app.core.timezones import zone

SESSION_KEY = "organization_id"


class OrganizationMiddleware:
    """Attach the current organization and the user's `Access` to every request, and run the
    request in the organization's time zone (D119).

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
        if request.organization is not None:
            timezone.activate(zone(request.organization))
        else:
            timezone.deactivate()  # the server default, before signing in
        try:
            return self.get_response(request)
        finally:
            timezone.deactivate()  # nothing carries over to the thread's next request
