# SPDX-License-Identifier: AGPL-3.0-or-later
"""Print a new VAPID key for phone notifications (D125). Put the line it prints in `.env`
(or a secret file named by STACKS_VAPID_PRIVATE_KEY_FILE), and keep it: a new key means every
device has to turn notifications on again."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from adapters import webpush


class Command(BaseCommand):
    help = "Print a new VAPID private key for phone notifications (web push)."

    def handle(self, *args, **options):
        key = webpush.generate_vapid_key()
        public = webpush.public_key_text(webpush.private_key_from_text(key))
        self.stdout.write(f"STACKS_VAPID_PRIVATE_KEY={key}")
        self.stdout.write(
            self.style.NOTICE(
                f"# Public key, sent to browsers automatically: {public}\n"
                "# Keep the private key secret and keep it the same: changing it turns notifications\n"
                "# off on every device until each person turns them on again."
            )
        )
