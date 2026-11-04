# SPDX-License-Identifier: AGPL-3.0-or-later
from types import SimpleNamespace

from django.conf import settings

from app.core import navigation
from app.core.permissions import Perm

PERM_NAMES = SimpleNamespace(**{p.name: p.value for p in Perm})  # templates would call the Enum class


def _push_check_url(request, user) -> str:
    """Signed-in pages check this device's phone notifications (D125), when the site has push."""
    from django.urls import reverse

    from app.scheduling import push

    if user is None or not user.is_authenticated or getattr(request, "organization", None) is None:
        return ""
    return reverse("push_check") if push.enabled() else ""


def _push_pending(user) -> bool:
    from app.scheduling.models import PushSubscription

    return PushSubscription.objects.filter(user=user, confirmed_at__isnull=True).exists()


def organization(request):
    unread = 0
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        unread = user.notifications.filter(read_at__isnull=True).count()
    push_url = _push_check_url(request, user)
    return {
        "unread_notifications": unread,
        "organization": getattr(request, "organization", None),
        "access": getattr(request, "access", None),
        "Perm": PERM_NAMES,
        "site_name": settings.STACKS_SITE_NAME,
        "remember_days": settings.STACKS_REMEMBER_DAYS,
        "push_check_url": push_url,
        "push_pending": bool(push_url) and _push_pending(user),
        "nav": navigation.build(request, unread),
    }
