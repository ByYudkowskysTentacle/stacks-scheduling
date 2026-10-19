# SPDX-License-Identifier: AGPL-3.0-or-later
from django import template

from domain.timeutil import format_minute_12h

register = template.Library()


@register.filter
def clock(minute):
    """Minutes since midnight as `7:30am`."""
    if minute is None or minute == "":
        return ""
    return format_minute_12h(int(minute))


@register.filter
def hours(minutes):
    """Minutes as hours, dropping a trailing `.0`: 90 -> 1.5."""
    if minutes is None:
        return ""
    value = int(minutes) / 60
    return f"{value:g}"
