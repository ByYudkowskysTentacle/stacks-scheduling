# SPDX-License-Identifier: AGPL-3.0-or-later
from django.conf import settings

from app.core.permissions import Perm


def organization(request):
    return {
        "organization": getattr(request, "organization", None),
        "access": getattr(request, "access", None),
        "Perm": Perm,
        "site_name": settings.STACKS_SITE_NAME,
    }
