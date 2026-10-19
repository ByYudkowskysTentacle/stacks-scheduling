# SPDX-License-Identifier: AGPL-3.0-or-later
"""Daily maintenance in the worker."""

from __future__ import annotations

from procrastinate.contrib.django import app

from app.core import retention


@app.periodic(cron="17 3 * * *")
@app.task(queue="maintenance", name="core.retention_purge", queueing_lock="retention_purge")
def retention_purge(timestamp: int) -> None:
    """Apply each organization's retention policy (SPEC §11). Organizations without one, or
    with every period blank, are left alone."""
    retention.purge_all()
