# SPDX-License-Identifier: AGPL-3.0-or-later
"""First-run setup: one organization, one location and an admin account.

    python manage.py bootstrap --organization "Example Library" --admin-email admin@example.edu

The admin password is read from STACKS_ADMIN_PASSWORD if set, otherwise prompted for.
Running it again is safe: existing rows are kept.
"""

from __future__ import annotations

import getpass
import os

import pghistory
from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from app.accounts.models import User
from app.core.models import Department, Location, Membership, Organization, Role
from app.core.timezones import validate_time_zone


class Command(BaseCommand):
    help = "Create the organization, its first location and an admin account."

    def add_arguments(self, parser):
        parser.add_argument("--organization", required=True)
        parser.add_argument("--location", default="Main library")
        parser.add_argument("--admin-email", required=True)
        parser.add_argument(
            "--time-zone",
            help="An IANA name such as America/Chicago. Defaults to STACKS_TIME_ZONE; change it later under Settings.",
        )

    def handle(self, *args, **opts):
        time_zone = opts["time_zone"] or settings.TIME_ZONE
        try:
            validate_time_zone(time_zone)
        except ValidationError as exc:
            raise CommandError(
                f"Unknown time zone {time_zone!r}. Use an IANA name such as America/Chicago."
            ) from exc
        email = opts["admin_email"].strip().lower()
        user = User.objects.filter(email=email).first()
        password = None
        if user is None:
            password = os.environ.get("STACKS_ADMIN_PASSWORD") or getpass.getpass(f"Password for {email}: ")
            try:
                validate_password(password)
            except ValidationError as exc:
                raise CommandError(" ".join(exc.messages)) from exc
        with transaction.atomic(), pghistory.context(command="bootstrap"):
            org, _ = Organization.objects.get_or_create(
                name=opts["organization"], defaults={"time_zone": time_zone}
            )
            Location.objects.get_or_create(organization=org, name=opts["location"])
            if not org.departments.exists():  # one to start; admins can rename it or add more (D95)
                Department.objects.get_or_create(organization=org, name="General")
            if user is None:
                user = User.objects.create_user(email, password)
            Membership.objects.update_or_create(user=user, organization=org, defaults={"role": Role.ADMIN})
        self.stdout.write(self.style.SUCCESS(f"Ready: {org.name}, admin {email}."))
