# SPDX-License-Identifier: AGPL-3.0-or-later
from types import SimpleNamespace

from django.conf import settings

from app.core.permissions import Perm

PERM_NAMES = SimpleNamespace(**{p.name: p.value for p in Perm})  # templates would call the Enum class


def organization(request):
    unread = 0
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        unread = user.notifications.filter(read_at__isnull=True).count()
    return {
        "unread_notifications": unread,
        "organization": getattr(request, "organization", None),
        "access": getattr(request, "access", None),
        "Perm": PERM_NAMES,
        "site_name": settings.STACKS_SITE_NAME,
    }
