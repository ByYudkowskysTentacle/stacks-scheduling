# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from procrastinate.contrib.django import app

from app.availability import reminders


@app.periodic(cron="5 * * * *")  # hourly; each organization's reminders wait for its local morning
@app.task(queue="maintenance", name="availability.send_reminders", queueing_lock="availability_reminders")
def send_reminders(timestamp: int) -> None:
    """Availability reminders for terms where a supervisor turned them on (SPEC §5.2), from 9am
    in each organization's time zone (D119)."""
    reminders.send_due()
