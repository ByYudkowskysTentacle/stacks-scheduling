# SPDX-License-Identifier: AGPL-3.0-or-later
"""Parsers for the documented, tool-agnostic roster and availability CSV formats (D13).

The formats are described in `docs/formats/`. These parsers are pure: they turn file
text into typed rows plus row-numbered problems, and never touch the database. Problem
messages name the row and column, never the cell value, so they are safe to log.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

from domain.availability import BlockKind
from domain.calendar import WEEKDAY_NAMES, WEEKDAY_SHORT
from domain.timeutil import DAY_MINUTES, Interval, parse_clock

ROSTER_REQUIRED = ("full_name", "preferred_name", "email")
ROSTER_OPTIONAL = (
    "campus_id",
    "employment_type",
    "hire_date",
    "new_employee_until",
    "default_pay_role",
    "tags",
)
AVAILABILITY_REQUIRED = ("email", "weekday", "type")
AVAILABILITY_OPTIONAL = ("start", "end")
EMPLOYMENT_TYPES = ("regular", "non_regular", "inactive")
MAX_ROWS = 5000

_WEEKDAYS = {
    **{name.lower(): i for i, name in enumerate(WEEKDAY_NAMES)},
    **{short.lower(): i for i, short in enumerate(WEEKDAY_SHORT)},
    "tues": 1,
    "weds": 2,
    "thur": 3,
    "thurs": 3,
}


@dataclass(frozen=True)
class Problem:
    row: int  # 1-based line number in the file, counting the header as row 1
    column: str
    message: str

    def __str__(self) -> str:
        where = f"row {self.row}" if self.row else "file"
        return f"{where}, {self.column}: {self.message}" if self.column else f"{where}: {self.message}"


@dataclass(frozen=True)
class RosterRow:
    row: int
    full_name: str
    preferred_name: str
    email: str
    campus_id: str = ""
    employment_type: str = "regular"
    hire_date: date | None = None
    new_employee_until: date | None = None
    default_pay_role: str = ""
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class AvailabilityRow:
    row: int
    email: str
    weekday: int
    kind: BlockKind
    interval: Interval


@dataclass
class ParseResult[T]:
    rows: list[T] = field(default_factory=lambda: [])
    problems: list[Problem] = field(default_factory=lambda: [])
    warnings: list[Problem] = field(default_factory=lambda: [])

    @property
    def ok(self) -> bool:
        return not self.problems


def _reader(
    text: str, required: Iterable[str], optional: Iterable[str]
) -> tuple[list[dict[str, str]], list[Problem], list[Problem]]:
    """Rows as lower-cased column dicts, blocking problems, and warnings."""
    text = text.lstrip("﻿")  # Excel's UTF-8 byte-order mark
    reader = csv.DictReader(io.StringIO(text))
    headers = [h.strip().lower() for h in (reader.fieldnames or [])]
    missing = [h for h in required if h not in headers]
    if missing:
        return [], [Problem(1, "", f"missing column(s): {', '.join(missing)}")], []
    known = set(required) | set(optional)
    warnings = [Problem(1, h, "unknown column, ignored") for h in headers if h and h not in known]
    rows: list[dict[str, str]] = []
    for raw in reader:
        rows.append({(k or "").strip().lower(): (v or "").strip() for k, v in raw.items() if k is not None})
        if len(rows) > MAX_ROWS:
            return [], [Problem(0, "", f"more than {MAX_ROWS} rows; split the file")], []
    return rows, [], warnings


def _date(value: str, row: int, column: str, problems: list[Problem]) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        problems.append(Problem(row, column, "use a date like 2027-01-11"))
        return None


def _email(value: str) -> str:
    return value.strip().lower()


def parse_roster(text: str) -> ParseResult[RosterRow]:
    result = ParseResult[RosterRow]()
    rows, result.problems, result.warnings = _reader(text, ROSTER_REQUIRED, ROSTER_OPTIONAL)
    seen: dict[str, int] = {}
    for i, r in enumerate(rows, start=2):
        problems: list[Problem] = []
        for col in ROSTER_REQUIRED:
            if not r.get(col):
                problems.append(Problem(i, col, "required"))
        email = _email(r.get("email", ""))
        if email and ("@" not in email or " " in email):
            problems.append(Problem(i, "email", "not an email address"))
        if email in seen:
            problems.append(Problem(i, "email", f"same email as row {seen[email]}"))
        seen.setdefault(email, i)
        etype = (r.get("employment_type") or "regular").lower().replace("-", "_").replace(" ", "_")
        if etype not in EMPLOYMENT_TYPES:
            problems.append(Problem(i, "employment_type", f"use one of {', '.join(EMPLOYMENT_TYPES)}"))
        hire = _date(r.get("hire_date", ""), i, "hire_date", problems)
        new_until = _date(r.get("new_employee_until", ""), i, "new_employee_until", problems)
        tags = tuple(t.strip() for t in (r.get("tags") or "").split(";") if t.strip())
        if problems:
            result.problems += problems
            continue
        result.rows.append(
            RosterRow(
                row=i,
                full_name=r["full_name"],
                preferred_name=r["preferred_name"],
                email=email,
                campus_id=r.get("campus_id", ""),
                employment_type=etype,
                hire_date=hire,
                new_employee_until=new_until,
                default_pay_role=r.get("default_pay_role", ""),
                tags=tags,
            )
        )
    return result


def parse_availability(text: str) -> ParseResult[AvailabilityRow]:
    """One row per block. Blank start and end mean all day. Times are kept exactly, even
    off the 15-minute grid (SPEC §3.3): scheduling rounds them toward unavailability."""
    result = ParseResult[AvailabilityRow]()
    rows, result.problems, result.warnings = _reader(text, AVAILABILITY_REQUIRED, AVAILABILITY_OPTIONAL)
    for i, r in enumerate(rows, start=2):
        problems: list[Problem] = []
        email = _email(r.get("email", ""))
        if not email:
            problems.append(Problem(i, "email", "required"))
        weekday = _WEEKDAYS.get(r.get("weekday", "").lower())
        if weekday is None:
            problems.append(Problem(i, "weekday", "use a day name such as Monday or Mon"))
        kind_text = r.get("type", "").lower()
        kind = {"unavailable": BlockKind.UNAVAILABLE, "preferred": BlockKind.PREFERRED}.get(kind_text)
        if kind is None:
            problems.append(Problem(i, "type", "use unavailable or preferred"))
        start_text, end_text = r.get("start", ""), r.get("end", "")
        interval: Interval | None = None
        if not start_text and not end_text:
            interval = Interval(0, DAY_MINUTES)
        elif not start_text or not end_text:
            problems.append(
                Problem(i, "end" if start_text else "start", "give both times, or neither for all day")
            )
        else:
            try:
                start, end = parse_clock(start_text), parse_clock(end_text)
                if start >= end:
                    problems.append(Problem(i, "end", "ends before it starts"))
                else:
                    interval = Interval(start, end)
            except ValueError:
                problems.append(Problem(i, "start", "use times like 09:00 or 9:50am"))
        if problems or weekday is None or kind is None or interval is None:
            result.problems += problems
            continue
        result.rows.append(AvailabilityRow(row=i, email=email, weekday=weekday, kind=kind, interval=interval))
    return result
