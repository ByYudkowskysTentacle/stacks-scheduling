# SPDX-License-Identifier: AGPL-3.0-or-later
from allauth.account.adapter import DefaultAccountAdapter


class InviteOnlyAccountAdapter(DefaultAccountAdapter):
    """No public sign-up: supervisors invite people (SPEC §11, local accounts)."""

    def is_open_for_signup(self, request) -> bool:
        return False
