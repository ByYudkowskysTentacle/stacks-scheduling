# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from django.core.management.base import BaseCommand

from app.core import retention
from app.core.models import RetentionPolicy


class Command(BaseCommand):
    help = "Apply retention policies now (the worker also runs this daily). --dry-run only counts."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        for policy in RetentionPolicy.objects.select_related("organization"):
            org = policy.organization
            if opts["dry_run"]:
                for due in retention.preview(org):
                    if due.days:
                        self.stdout.write(f"{org.name}: {due.label}: {due.count} due (after {due.days} days)")
            else:
                counts = retention.purge(org)
                self.stdout.write(
                    f"{org.name}: " + (", ".join(f"{k} {v}" for k, v in counts.items()) or "nothing set")
                )
