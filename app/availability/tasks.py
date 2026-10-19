# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from django.utils import timezone
from procrastinate.contrib.django import app

from app.availability import reminders


@app.periodic(cron="5 14 * * *")  # mid-morning in US time zones
@app.task(queue="maintenance", name="availability.send_reminders", queueing_lock="availability_reminders")
def send_reminders(timestamp: int) -> None:
    """Availability reminders for terms where a supervisor turned them on (SPEC §5.2)."""
    reminders.send_due(timezone.localdate())
