# SPDX-License-Identifier: AGPL-3.0-or-later
from types import SimpleNamespace

from django.conf import settings

from app.core.permissions import Perm

PERM_NAMES = SimpleNamespace(**{p.name: p.value for p in Perm})  # templates would call the Enum class


def organization(request):
    return {
        "organization": getattr(request, "organization", None),
        "access": getattr(request, "access", None),
        "Perm": PERM_NAMES,
        "site_name": settings.STACKS_SITE_NAME,
    }
