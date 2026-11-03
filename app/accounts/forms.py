# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from allauth.account.forms import LoginForm as AllauthLoginForm
from django.conf import settings

from app.accounts import sessions


class LoginForm(AllauthLoginForm):
    """allauth's sign-in form, with its "Remember me" box as "Stay signed in" (D123)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "remember" in self.fields:
            self.fields["remember"].label = "Stay signed in"
            self.fields["remember"].help_text = (
                f"On your own phone or computer. You stay signed in until you sign out, or for "
                f"{settings.STACKS_REMEMBER_DAYS} days after you last use {settings.STACKS_SITE_NAME}."
            )

    def login(self, request, redirect_url=None):
        response = super().login(request, redirect_url)
        if self.cleaned_data.get("remember"):
            sessions.stay_signed_in(request.session)  # allauth set its own, shorter expiry
        else:
            sessions.forget(request.session)
        return response
