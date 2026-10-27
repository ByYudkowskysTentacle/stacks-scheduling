# SPDX-License-Identifier: AGPL-3.0-or-later
"""The organization's time zone (D119).

Shift times are wall-clock times in the organization's zone. Requests run in it through
`OrganizationMiddleware`; background jobs, calendar feeds and anything else outside a request
use `org_time()` or `local_today()` here, never the server default.
"""

from __future__ import annotations

import functools
import zoneinfo
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

# Regions of the canonical IANA names. Legacy and fixed-offset names (US/Central, Etc/GMT+6) are left
# out of the list, but a stored one stays valid.
REGIONS = (
    "Africa",
    "America",
    "Antarctica",
    "Arctic",
    "Asia",
    "Atlantic",
    "Australia",
    "Europe",
    "Indian",
    "Pacific",
)


@functools.cache
def zone_names() -> frozenset[str]:
    return frozenset(zoneinfo.available_timezones())


def default_time_zone() -> str:
    """New organizations start in the installation's `STACKS_TIME_ZONE`."""
    return settings.TIME_ZONE


def validate_time_zone(value: str) -> None:
    if value not in zone_names():
        raise ValidationError("Choose a time zone from the list.")


def zone_label(name: str) -> str:
    """`America/Argentina/Buenos_Aires` reads as `America/Argentina/Buenos Aires`: the whole
    name, so a closed select still says which region it is."""
    return name.replace("_", " ")


def time_zone_choices(current: str = "") -> list:
    """Grouped choices for a select: UTC, then each region. A stored legacy name is kept."""
    names = zone_names()
    groups: list = [("UTC", "UTC")]
    for region in REGIONS:
        zones = sorted(n for n in names if n.split("/", 1)[0] == region and "/" in n)
        groups.append((region, [(n, zone_label(n)) for n in zones]))
    listed = {"UTC"} | {n for n in names if n.split("/", 1)[0] in REGIONS and "/" in n}
    if current and current not in listed:
        # Kept so the select shows what's stored; one the system doesn't know fails validation,
        # so saving asks for a real zone rather than silently switching to the first one.
        groups.append(("Other", [(current, current)]))
    return groups


def zone_named(name: str) -> zoneinfo.ZoneInfo:
    """A zone by name. One the system no longer knows falls back to the installation default."""
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return zoneinfo.ZoneInfo(settings.TIME_ZONE)


def zone(org) -> zoneinfo.ZoneInfo:
    """The organization's zone."""
    return zone_named(org.time_zone)


def local_now(org) -> datetime:
    return timezone.now().astimezone(zone(org))


def local_today(org) -> date:
    return local_now(org).date()


@contextmanager
def org_time(org) -> Iterator[None]:
    """Run a block in the organization's zone: `timezone.localdate()`, date lookups and
    rendered times all follow it. For work outside a request (D119)."""
    with timezone.override(zone(org)):
        yield
