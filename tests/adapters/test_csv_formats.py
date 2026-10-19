# SPDX-License-Identifier: AGPL-3.0-or-later
from datetime import date
from pathlib import Path

from adapters.csv_formats import parse_availability, parse_roster
from domain.availability import BlockKind
from domain.calendar import MON, SAT, TUE
from domain.timeutil import Interval

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "imports"


def test_sample_roster_parses():
    result = parse_roster((FIXTURES / "roster.csv").read_text())
    assert result.ok, result.problems
    assert len(result.rows) == 4
    blake = result.rows[1]
    assert blake.tags == ("Shift lead", "Cataloguer")
    assert result.rows[2].new_employee_until == date(2027, 2, 28)
    assert result.rows[3].employment_type == "non_regular"


def test_sample_availability_parses():
    result = parse_availability((FIXTURES / "availability.csv").read_text())
    assert result.ok, result.problems
    first = result.rows[0]
    assert (first.weekday, first.kind, first.interval) == (MON, BlockKind.UNAVAILABLE, Interval(540, 590))
    sat = result.rows[3]
    assert (sat.weekday, sat.interval) == (SAT, Interval(0, 1440))
    tue = result.rows[5]
    assert (tue.weekday, tue.interval) == (TUE, Interval(570, 645))


def test_missing_columns_are_reported():
    result = parse_roster("name,email\nA,a@example.edu\n")
    assert not result.ok
    assert "missing column" in str(result.problems[0])


def test_roster_row_problems_name_row_and_column_but_not_the_value():
    text = (
        "full_name,preferred_name,email,hire_date,employment_type\nA B,A,not-an-email,2027-13-01,contractor\n"
    )
    result = parse_roster(text)
    messages = [str(p) for p in result.problems]
    assert any("row 2, email" in m for m in messages)
    assert any("row 2, hire_date" in m for m in messages)
    assert any("row 2, employment_type" in m for m in messages)
    assert not any("not-an-email" in m or "contractor" in m for m in messages)
    assert result.rows == []


def test_duplicate_emails_are_rejected():
    text = "full_name,preferred_name,email\nA B,A,a@example.edu\nC D,C,A@example.edu\n"
    result = parse_roster(text)
    assert [str(p) for p in result.problems] == ["row 3, email: same email as row 2"]


def test_unknown_columns_warn_without_blocking():
    result = parse_roster("full_name,preferred_name,email,shoe_size\nA B,A,a@example.edu,9\n")
    assert result.ok and len(result.rows) == 1
    assert "unknown column" in str(result.warnings[0])


def test_availability_problems():
    text = (
        "email,weekday,type,start,end\n"
        "a@example.edu,Funday,maybe,10:00,\n"
        "a@example.edu,Mon,unavailable,11:00,10:00\n"
    )
    result = parse_availability(text)
    messages = [str(p) for p in result.problems]
    assert "row 2, weekday: use a day name such as Monday or Mon" in messages
    assert "row 2, type: use unavailable or preferred" in messages
    assert any(m.startswith("row 2, end") for m in messages)
    assert "row 3, end: ends before it starts" in messages


def test_excel_byte_order_mark_is_ignored():
    result = parse_roster("﻿full_name,preferred_name,email\nA B,A,a@example.edu\n")
    assert result.ok
