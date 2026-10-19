# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wall-clock times are stored as minutes since local midnight (0 to 1440), the same
representation `domain.timeutil` uses, so 24:00 is expressible and nothing shifts at DST."""

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from domain.timeutil import DAY_MINUTES


def minute_field(verbose_name: str, **kwargs) -> models.PositiveSmallIntegerField:
    return models.PositiveSmallIntegerField(
        verbose_name,
        validators=[MinValueValidator(0), MaxValueValidator(DAY_MINUTES)],
        **kwargs,
    )
