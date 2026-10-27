# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import logging

from allauth.account.adapter import DefaultAccountAdapter
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.providers.base import AuthProcess
from django.conf import settings
from django.shortcuts import render

from app.accounts.models import User

logger = logging.getLogger(__name__)

# The only claims kept from the identity provider (SPEC §11: collect the minimum).
KEPT_CLAIMS = ("iss", "sub", "email", "email_verified", "hd")


class InviteOnlyAccountAdapter(DefaultAccountAdapter):
    """No public sign-up: supervisors invite people (SPEC §11, local accounts)."""

    def is_open_for_signup(self, request) -> bool:
        return False


def _claims(sociallogin) -> dict:
    """The provider's claims, preferring the signed ID token over the userinfo response."""
    data = sociallogin.account.extra_data or {}
    return {**data.get("userinfo", {}), **data.get("id_token", {})}


def _is_true(value) -> bool:
    return value is True or str(value).lower() == "true"


def _minimise(sociallogin) -> None:
    data = sociallogin.account.extra_data or {}
    sociallogin.account.extra_data = {
        part: {k: v for k, v in data[part].items() if k in KEPT_CLAIMS}
        for part in ("userinfo", "id_token")
        if isinstance(data.get(part), dict)
    }


class CampusSSOAdapter(DefaultSocialAccountAdapter):
    """Campus sign-in over OpenID Connect (D5, D118).

    It signs in people who already have a Stacks account, matched by email address, and
    never creates one. Before anything else the provider must vouch for the address
    (`email_verified`) and the address must be in an allowed campus domain. With
    OIDC_REQUIRE_HD (Google Workspace) the account must also belong to that Workspace:
    the `hd` claim, checked here rather than trusted from the request."""

    def is_open_for_signup(self, request, sociallogin) -> bool:
        return False

    def pre_social_login(self, request, sociallogin) -> None:
        claims = _claims(sociallogin)
        # For an account that's already linked, allauth has just saved the provider's whole
        # response on it (its lookup runs first). Trim and save before any check below can turn
        # the person away, because a refusal still commits the request.
        _minimise(sociallogin)
        if sociallogin.is_existing:
            sociallogin.account.save(update_fields=["extra_data"])

        email = str(claims.get("email") or "").strip().lower()
        name = sociallogin.provider.name
        if not email or not _is_true(claims.get("email_verified")):
            self._refuse(
                request, name, "Your campus sign-in didn't confirm an email address for this account."
            )
        allowed = settings.OIDC_ALLOWED_DOMAINS
        if email.rpartition("@")[2] not in allowed:
            self._refuse(request, name, "Use your campus account. This one isn't from your campus.")
        if settings.OIDC_REQUIRE_HD and claims.get("hd") not in allowed:
            if not claims.get("hd"):
                # Usually a non-Google provider with OIDC_REQUIRE_HD left on. No personal data here.
                logger.warning("Campus sign-in refused: no hd claim, and OIDC_REQUIRE_HD is on")
            self._refuse(
                request,
                name,
                "This account isn't part of your campus's Google Workspace. If you used your campus "
                "account, tell your Stacks administrator.",
            )

        if sociallogin.state.get("process") == AuthProcess.CONNECT:
            if not request.user.is_authenticated or request.user.email.lower() != email:
                self._refuse(request, name, "That account's email address doesn't match your Stacks account.")
            return
        if sociallogin.is_existing:
            return
        user = User.objects.filter(email__iexact=email, is_active=True).first()
        if user is None:
            self._refuse(
                request,
                name,
                f"No Stacks account uses {email}. Ask your supervisor to invite you, then sign in again.",
            )
        sociallogin.connect(request, user)

    def _refuse(self, request, provider_name: str, reason: str):
        response = render(
            request,
            "account/sso_refused.html",
            {"provider_name": provider_name, "reason": reason},
            status=403,
        )
        raise ImmediateHttpResponse(response)
