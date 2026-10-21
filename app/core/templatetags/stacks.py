# SPDX-License-Identifier: AGPL-3.0-or-later
import re

from django import template

from domain.calendar import WEEKDAY_SHORT
from domain.timeutil import format_minute_12h

register = template.Library()


@register.filter
def clock(minute):
    """Minutes since midnight as `7:30am`."""
    if minute is None or minute == "":
        return ""
    return format_minute_12h(int(minute))


@register.filter
def weekday_short(weekday):
    return WEEKDAY_SHORT[int(weekday)]


@register.filter
def get_item(mapping, key):
    return mapping.get(key) if mapping else None


@register.filter
def index(sequence, i):
    return sequence[int(i)]


@register.filter
def hours(minutes):
    """Minutes as hours, dropping a trailing `.0`: 90 -> 1.5."""
    if minutes is None:
        return ""
    value = int(minutes) / 60
    return f"{value:g}"


@register.filter
def reachable_count(due):
    """How many people in an availability-reminder list can actually be emailed."""
    return sum(1 for d in due if d.can_remind)


@register.filter
def without_prefix(text, prefix):
    """ "Umi Tue 19 Jan: 8h in a day" -> "Tue 19 Jan: 8h in a day" in a row already headed "Umi".
    A sentence with the name as its subject ("Umi is unavailable…") is left whole."""
    text, prefix = str(text), f"{prefix} "
    rest = text[len(prefix) :] if text.startswith(prefix) else ""
    return rest if rest and not rest[0].islower() else text


URL_LINE = re.compile(r"(https?://\S+|\s/[\w/.-]*)\s*$")


@register.filter
def in_app_body(text):
    """A notification's text without the lines that only carry a link: emails need them, the
    app shows a proper link underneath instead (audit 3.7)."""
    return "\n".join(line for line in str(text).splitlines() if not URL_LINE.search(line)).strip()


LINK_LABELS = {
    "/my-schedule/": "See my shifts",
    "/published/": "See the schedule",
    "/shifts/exchange/": "Open shifts and swaps",
    "/time-off/": "See my time off",
    "/availability/": "Enter my availability",
    "/notifications/email/": "Email settings",
}


@register.filter
def link_label(url):
    """Link text that says where a notification's link goes, instead of "Open"."""
    path = str(url).split("?")[0]
    return next((label for prefix, label in LINK_LABELS.items() if path.endswith(prefix)), "Open")


@register.filter
def strip_prefix(text, prefix):
    """ "Stacks: your schedule…" -> "your schedule…" (then capfirst in the template)."""
    text = str(text)
    return text[len(prefix) :].lstrip() if text.startswith(str(prefix)) else text
