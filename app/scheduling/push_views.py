# SPDX-License-Identifier: AGPL-3.0-or-later
"""Turning phone notifications on and off for a device (D125).

The page script and service worker talk to these in JSON. Turning on, the sign-in check and
the test need the person signed in with a CSRF token. Dropping and renewing a device come from
the service worker, which has neither at sign-out or when the browser renews a subscription,
so they need the device's endpoint and its secret `auth` key instead: only the browser and
this server know that key (the push service never sees it).
"""

from __future__ import annotations

import hmac
import json

from django.contrib import messages
from django.db.models import F, Value
from django.db.models.functions import Coalesce
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from adapters import webpush
from adapters.webpush import Subscription
from app.core.views_base import OrgViewMixin
from app.scheduling import push
from app.scheduling.models import PushSubscription

MAX_ENDPOINT = 1000


def _json(request) -> dict:
    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _subscription(data: object) -> Subscription | None:
    """A browser subscription (`PushSubscription.toJSON()`), checked: an allowed push service
    and well-formed keys. None if anything is off."""
    if not isinstance(data, dict):
        return None
    keys = data.get("keys")
    endpoint = data.get("endpoint")
    if not isinstance(keys, dict) or not isinstance(endpoint, str) or len(endpoint) > MAX_ENDPOINT:
        return None
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    if not isinstance(p256dh, str) or not isinstance(auth, str):
        return None
    sub = Subscription(endpoint, p256dh.strip(), auth.strip())
    if not webpush.endpoint_allowed(endpoint, push.hosts()):
        return None
    try:
        sub.keys()
    except ValueError:
        return None
    return sub


def _device(data: dict) -> PushSubscription | None:
    """The stored device named by an endpoint and its auth secret, or None."""
    endpoint, auth = data.get("endpoint"), data.get("auth")
    if not isinstance(endpoint, str) or not isinstance(auth, str) or len(endpoint) > MAX_ENDPOINT:
        return None
    sub = PushSubscription.objects.filter(endpoint=endpoint).first()
    if sub is None or not hmac.compare_digest(sub.auth.encode(), auth.strip().encode()):
        return None
    return sub


class PushEnabledMixin:
    def dispatch(self, request, *args, **kwargs):
        if not push.enabled():
            raise Http404
        return super().dispatch(request, *args, **kwargs)  # type: ignore[misc]


class PushSubscribeView(PushEnabledMixin, OrgViewMixin, View):
    """Turn notifications on for this device, for the signed-in person."""

    def post(self, request):
        sub = _subscription(_json(request))
        if sub is None:
            return HttpResponseBadRequest("Not a subscription Stacks can use.")
        push.save_device(request.user, sub, request.headers.get("User-Agent", ""))
        return JsonResponse({"state": "on"}, status=201)


class PushCheckView(PushEnabledMixin, OrgViewMixin, View):
    """Once a day, and after each sign-in, the page asks whether this device's notifications
    are the signed-in person's. A device someone else turned on is dropped, so their news
    never reaches the next person to sign in on it."""

    def post(self, request):
        data = _json(request)
        sub = _device(data)
        if sub is not None and sub.user_id == request.user.pk:
            now = timezone.now()
            PushSubscription.objects.filter(pk=sub.pk).update(
                last_seen_at=now, confirmed_at=Coalesce(F("confirmed_at"), Value(now))
            )
            return JsonResponse({"state": "on"})
        if sub is not None:
            sub.delete()
        return JsonResponse({"state": "off"})


@method_decorator(csrf_exempt, name="dispatch")
class PushDropView(PushEnabledMixin, View):
    """Turn notifications off for this device: from the settings page, and from the service
    worker when the person signs out."""

    def post(self, request):
        sub = _device(_json(request))
        if sub is not None:
            sub.delete()
        return HttpResponse(status=204)


@method_decorator(csrf_exempt, name="dispatch")
class PushRenewView(PushEnabledMixin, View):
    """The browser replaced a subscription (`pushsubscriptionchange`): move the device to the
    new one, proven by the old one's endpoint and secret. Anyone with the browser could read
    those, so the new subscription gets nothing until the owner next opens Stacks on that
    device signed in (the page's check confirms it)."""

    def post(self, request):
        data = _json(request)
        old = data.get("old")
        sub = _device(old) if isinstance(old, dict) else None
        if sub is None:
            raise Http404
        new = _subscription(data.get("subscription"))
        if new is None:
            return HttpResponseBadRequest("Not a subscription Stacks can use.")
        user = sub.user
        sub.delete()
        push.save_device(user, new, request.headers.get("User-Agent", ""), confirmed=False)
        return HttpResponse(status=204)


class PushDeviceRemoveView(PushEnabledMixin, OrgViewMixin, View):
    """Remove one of the person's own devices from the list (for a lost or old phone)."""

    def post(self, request, pk: int):
        sub = get_object_or_404(PushSubscription, pk=pk, user=request.user)
        name = sub.device or "The device"
        sub.delete()
        messages.success(request, f"{name} won't get notifications any more.")
        return redirect("email_settings")


class PushTestView(PushEnabledMixin, OrgViewMixin, View):
    """Send a test to the person's own devices, to check that notifications arrive."""

    def post(self, request):
        n = push.queue_test(request.user)
        if n:
            messages.success(
                request,
                f"Test notification sent to {n} device{'s' if n != 1 else ''}. It can take a minute to arrive.",
            )
        else:
            messages.info(request, "No devices have notifications turned on yet.")
        return redirect("email_settings")
