# SPDX-License-Identifier: AGPL-3.0-or-later
"""Background tasks, run by the `worker` container (Procrastinate, ADR 0001)."""

from procrastinate.contrib.django import app

# The worker serves every queue one job at a time, highest priority first: a push goes ahead of
# any solves still waiting, though not of the one already running (D125).
PUSH_PRIORITY = 10


@app.task(queue="notifications", name="scheduling.send_push", priority=PUSH_PRIORITY)
def send_push(notification_id: int, subscription_id: int | None = None, attempt: int = 1) -> None:
    """Push one notification to the person's phones (D125)."""
    from app.scheduling.push import deliver

    deliver(notification_id, subscription_id, attempt)


@app.task(queue="notifications", name="scheduling.send_test_push", priority=PUSH_PRIORITY)
def send_test_push(user_id: int) -> None:
    """The test notification a person asked for from their notification settings (D125)."""
    from app.scheduling.push import deliver_test

    deliver_test(user_id)


@app.task(queue="solver", name="scheduling.run_solve")
def run_solve(run_id: int) -> None:
    from app.scheduling.solver import execute_run

    execute_run(run_id)
