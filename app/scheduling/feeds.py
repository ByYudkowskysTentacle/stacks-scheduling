# SPDX-License-Identifier: AGPL-3.0-or-later
"""The private ICS feed of an employee's own published shifts (SPEC §9.2).

Only published shifts appear, never drafts. Events carry the time, location, the co-workers
on at the same time (preferred names, as in the published view, D6) and the employee's own
shift note. Times are written in UTC so no time-zone table is needed.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pghistory
from django.utils import timezone

from app.core.timezones import local_today, zone
from app.people.models import Employee
from app.scheduling.models import CalendarFeed
from app.scheduling.published import my_upcoming

PAST_DAYS = 28
WEEKS_AHEAD = 16


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(employee: Employee) -> str:
    """Create or replace the employee's feed and return the token. Shown once."""
    token = secrets.token_urlsafe(32)
    with pghistory.context(action="calendar_feed_reset", employee=employee.pk):
        CalendarFeed.objects.update_or_create(
            employee=employee, defaults={"organization": employee.organization, "token_hash": _hash(token)}
        )
    return token


def employee_for(token: str) -> Employee | None:
    feed = (
        CalendarFeed.objects.filter(token_hash=_hash(token)).select_related("employee__organization").first()
    )
    if feed is None or feed.employee.is_archived:
        return None
    return feed.employee


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", r"\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """RFC 5545 §3.1: lines over 75 octets continue on the next line after a space."""
    raw = line.encode()
    if len(raw) <= 75:
        return line
    parts, start = [], 0
    while start < len(raw):
        end = min(start + (75 if start == 0 else 74), len(raw))
        while end < len(raw) and (raw[end] & 0xC0) == 0x80:  # don't split a UTF-8 character
            end -= 1
        parts.append(raw[start:end].decode())
        start = end
    return "\r\n ".join(parts)


def _utc(day: date, minute: int, tz: ZoneInfo) -> str:
    # Aware arithmetic with ZoneInfo is wall-clock arithmetic, so DST days get the right offset.
    local = datetime.combine(day, time(0), tzinfo=tz) + timedelta(minutes=minute)
    return local.astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")


def build_ics(employee: Employee, today: date | None = None) -> str:
    org = employee.organization
    tz = zone(org)  # shift times are wall-clock times in the organization's zone (D119)
    today = today or local_today(org)
    stamp = timezone.now().astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Stacks//Published shifts//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(org.name)} shifts",
        "X-PUBLISHED-TTL:PT1H",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
    ]
    for m in my_upcoming(employee, today - timedelta(days=PAST_DAYS), weeks=WEEKS_AHEAD + 5):
        s = m.shift
        uid = f"shift-{s.shift_id or f'{s.day:%Y%m%d}-{s.interval.start}'}-{employee.pk}@stacks"
        details = []
        if m.with_me:
            details.append("With: " + ", ".join(m.with_me))
        if s.note:
            details.append("Note: " + s.note)
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{_utc(s.day, s.interval.start, tz)}",
            f"DTEND:{_utc(s.day, s.interval.end, tz)}",
            f"SUMMARY:{_escape(f'Shift at {s.location}')}",
            f"LOCATION:{_escape(s.location)}",
        ]
        if details:
            lines.append(f"DESCRIPTION:{_escape(chr(10).join(details))}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
