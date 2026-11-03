# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stay signed in (D123). A sign-in with "Stay signed in" ticked lasts STACKS_REMEMBER_DAYS from
the person's last visit, so an installed phone app doesn't ask them to sign in every day; the
phone's own passcode guards it. Any other sign-in ends when the browser closes (password) or
after SESSION_COOKIE_AGE (campus sign-in), as before."""

from __future__ import annotations

from allauth.account.signals import user_logged_in
from django.conf import settings
from django.dispatch import receiver
from django.urls import Resolver404, resolve
from django.utils import timezone

REMEMBERED = "stay_signed_in"
RENEWED_ON = "stay_signed_in_renewed"
SSO_CHOICE = "stay_signed_in_sso"  # the campus sign-in box, kept while the person is at the provider
SSO_LOGIN_URL_NAME = "openid_connect_login"


def stay_signed_in(session) -> None:
    session.set_expiry(settings.STACKS_REMEMBER_DAYS * 24 * 60 * 60)
    session[REMEMBERED] = True
    session[RENEWED_ON] = timezone.localdate().isoformat()


def forget(session) -> None:
    session.pop(REMEMBERED, None)
    session.pop(RENEWED_ON, None)


@receiver(user_logged_in)
def _after_campus_sign_in(sender, request, user, **kwargs) -> None:
    """Password sign-ins are handled by the login form; this is the campus sign-in's turn."""
    if kwargs.get("sociallogin") is None:
        return
    if request.session.pop(SSO_CHOICE, False) and settings.STACKS_REMEMBER_DAYS:
        stay_signed_in(request.session)
    else:
        forget(request.session)


class StaySignedInMiddleware:
    """Notes the campus sign-in's "Stay signed in" box on the way to the provider, and keeps a
    remembered sign-in going: the first visit each day saves the session, which restarts its
    clock. Sits after the session and authentication middleware."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method == "POST" and _is_campus_sign_in(request.path_info):
            request.session[SSO_CHOICE] = request.POST.get("remember") == "on"
        response = self.get_response(request)
        session = getattr(request, "session", None)
        user = getattr(request, "user", None)
        if session is not None and user is not None and user.is_authenticated and session.get(REMEMBERED):
            today = timezone.localdate().isoformat()
            if session.get(RENEWED_ON) != today:
                session[RENEWED_ON] = today
        return response


def _is_campus_sign_in(path: str) -> bool:
    try:
        return resolve(path).url_name == SSO_LOGIN_URL_NAME
    except Resolver404:
        return False
