# SPDX-License-Identifier: AGPL-3.0-or-later
"""Background tasks, run by the `worker` container (Procrastinate, ADR 0001)."""

from procrastinate.contrib.django import app


@app.task(queue="solver", name="scheduling.run_solve")
def run_solve(run_id: int) -> None:
    from app.scheduling.solver import execute_run

    execute_run(run_id)
