# SPDX-License-Identifier: AGPL-3.0-or-later
from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "app.accounts"
    label = "accounts"
    verbose_name = "Accounts"

    def ready(self):
        from app.accounts import sessions  # noqa: F401  (connects the sign-in signal)
