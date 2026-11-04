# SPDX-License-Identifier: AGPL-3.0-or-later
from django.apps import AppConfig


class SchedulingConfig(AppConfig):
    name = "app.scheduling"
    label = "scheduling"
    verbose_name = "Scheduling"

    def ready(self) -> None:
        from app.scheduling import checks  # noqa: F401  (registers the push settings check)
