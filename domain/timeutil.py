# SPDX-License-Identifier: AGPL-3.0-or-later
"""Minutes, 15-minute slots and half-open time intervals (SPEC §3.3).

Times of day are whole minutes since local midnight, 0 to 1440 inclusive, so a
block can end at midnight. Everything works in the location's wall-clock time;
the web layer converts at its edge.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

SLOT_MINUTES = 15
SLOTS_PER_HOUR = 60 // SLOT_MINUTES
SLOTS_PER_DAY = 24 * SLOTS_PER_HOUR
DAY_MINUTES = 24 * 60


def is_on_slot(minute: int) -> bool:
    return minute % SLOT_MINUTES == 0


def slot_floor(minute: int) -> int:
    """The slot containing `minute` (a start rounded down)."""
    return minute // SLOT_MINUTES


def slot_ceil(minute: int) -> int:
    """The first slot boundary at or after `minute` (an end rounded up)."""
    return -(-minute // SLOT_MINUTES)


def format_minute(minute: int) -> str:
    """24-hour `HH:MM`; 1440 prints as `24:00`."""
    return f"{minute // 60:02d}:{minute % 60:02d}"


def format_minute_12h(minute: int) -> str:
    """Short 12-hour form used in the UI: `7:30am`, `12pm`, `10pm`, `midnight`."""
    if minute in (0, DAY_MINUTES):
        return "midnight"
    h, m = divmod(minute, 60)
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}{suffix}" if m == 0 else f"{h12}:{m:02d}{suffix}"


def parse_minute(text: str) -> int:
    """Parse `HH:MM` (24-hour, `24:00` allowed) into minutes since midnight."""
    hours, sep, minutes = text.strip().partition(":")
    if not sep or not hours.isdigit() or not minutes.isdigit() or len(minutes) != 2:
        raise ValueError(f"expected HH:MM, got {text!r}")
    value = int(hours) * 60 + int(minutes)
    if int(minutes) >= 60 or not 0 <= value <= DAY_MINUTES:
        raise ValueError(f"time out of range: {text!r}")
    return value


@dataclass(frozen=True, order=True)
class Interval:
    """A half-open interval of minutes [start, end) within one day."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if not 0 <= self.start < self.end <= DAY_MINUTES:
            raise ValueError(f"invalid interval {self.start}-{self.end}")

    @property
    def minutes(self) -> int:
        return self.end - self.start

    def overlaps(self, other: Interval) -> bool:
        return self.start < other.end and other.start < self.end

    def contains(self, other: Interval) -> bool:
        return self.start <= other.start and other.end <= self.end

    def label(self) -> str:
        return f"{format_minute_12h(self.start)}–{format_minute_12h(self.end)}"


def merge(intervals: Iterable[Interval]) -> list[Interval]:
    """Sort and merge overlapping or touching intervals."""
    out: list[Interval] = []
    for iv in sorted(intervals):
        if out and iv.start <= out[-1].end:
            if iv.end > out[-1].end:
                out[-1] = Interval(out[-1].start, iv.end)
        else:
            out.append(iv)
    return out


def subtract(base: Iterable[Interval], remove: Iterable[Interval]) -> list[Interval]:
    """`base` minus `remove`, merged."""
    result = merge(base)
    for cut in merge(remove):
        nxt: list[Interval] = []
        for iv in result:
            if not iv.overlaps(cut):
                nxt.append(iv)
                continue
            if iv.start < cut.start:
                nxt.append(Interval(iv.start, cut.start))
            if cut.end < iv.end:
                nxt.append(Interval(cut.end, iv.end))
        result = nxt
    return result


def intersect(a: Iterable[Interval], b: Iterable[Interval]) -> list[Interval]:
    out: list[Interval] = []
    bm = merge(b)
    for x in merge(a):
        for y in bm:
            lo, hi = max(x.start, y.start), min(x.end, y.end)
            if lo < hi:
                out.append(Interval(lo, hi))
    return merge(out)


def total_minutes(intervals: Iterable[Interval]) -> int:
    return sum(iv.minutes for iv in merge(intervals))


CLOCK_RE = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?\s*$", re.IGNORECASE)


def parse_clock(text: str) -> int:
    """Lenient time entry: `7:30`, `07:30`, `19:30`, `7:30pm`, `7pm`, `noon`, `midnight`, `24:00`."""
    t = text.strip().lower()
    if t == "noon":
        return 720
    if t == "midnight":
        return DAY_MINUTES
    m = CLOCK_RE.match(t)
    if not m:
        raise ValueError(f"not a time: {text!r}")
    hours, minutes = int(m.group(1)), int(m.group(2) or 0)
    suffix = (m.group(3) or "").replace(".", "")
    if minutes >= 60:
        raise ValueError(f"not a time: {text!r}")
    if suffix:
        if not 1 <= hours <= 12:
            raise ValueError(f"not a time: {text!r}")
        hours = hours % 12 + (12 if suffix == "pm" else 0)
    value = hours * 60 + minutes
    if value > DAY_MINUTES:
        raise ValueError(f"not a time: {text!r}")
    return value


def parse_range(text: str, *, on_slot: bool = True) -> Interval:
    """`07:30-22:00`, `8am to 12pm` or `9:00–9:50`."""
    bits = re.split(r"\s*(?:-|–|\bto\b)\s*", text.strip())
    if len(bits) != 2 or not all(bits):
        raise ValueError(f"expected a range like 07:30-22:00, got {text.strip()!r}")
    start, end = parse_clock(bits[0]), parse_clock(bits[1])
    if on_slot and not (is_on_slot(start) and is_on_slot(end)):
        raise ValueError("times must be on a 15-minute boundary")
    if start >= end:
        raise ValueError(f"{text.strip()!r} ends before it starts")
    return Interval(start, end)


def parse_ranges(text: str, *, on_slot: bool = True) -> list[Interval]:
    """Comma- or semicolon-separated ranges, merged. Blank gives an empty list."""
    return merge(parse_range(part, on_slot=on_slot) for part in re.split(r"[,;]", text) if part.strip())


def format_ranges(intervals: Iterable[Interval]) -> str:
    return ", ".join(f"{format_minute(iv.start)}-{format_minute(iv.end)}" for iv in merge(intervals))
