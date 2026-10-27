# SPDX-License-Identifier: AGPL-3.0-or-later
"""Planning periods of weeks, calendar months or a whole term (D120): creating them, the grid,
reports, versions and print in calendar weeks, caps across neighbouring periods, and solves on
long periods. One failing and one passing case per rule (CLAUDE.md §5)."""

from __future__ import annotations

import json
from datetime import date

import pytest
from django.urls import reverse

from app.core.models import Department, Term
from app.scheduling import services
from app.scheduling.models import PeriodLength, SchedulePeriod, Shift
from app.scheduling.published import my_upcoming
from app.scheduling.solve_views import SolveForm
from domain.validation import Category
from tests.conftest import manages

pytestmark = pytest.mark.django_db
FEB_1 = date(2027, 2, 1)  # a Monday; the week starts on Saturday


@pytest.fixture
def dept(org, supervisor):
    d = Department.objects.create(organization=org, name="Circulation")
    manages(supervisor, d)
    return d


@pytest.fixture
def term(org):
    return Term.objects.create(
        organization=org, name="Spring 2027", start_date=date(2027, 1, 12), end_date=date(2027, 5, 7)
    )


def month(org, location, dept, start=FEB_1, months=1):
    return SchedulePeriod.objects.create(
        organization=org,
        location=location,
        department=dept,
        length=PeriodLength.MONTHS,
        start_date=start,
        months=months,
    )


def add_shift(period, employee, day, start=600, end=840):
    return Shift.objects.create(
        organization=period.organization,
        location=period.location,
        period=period,
        employee=employee,
        date=day,
        start_min=start,
        end_min=end,
    )


def create(client, **data):
    return client.post(reverse("schedule_index"), data)


# --- Creating periods ------------------------------------------------------------------------


def test_a_month_period_starts_on_the_first_and_ends_with_its_month(
    as_user, supervisor, location, dept, term
):
    create(as_user(supervisor), department=dept.pk, length="months", start_date="2027-02-17", months=1)
    period = SchedulePeriod.objects.get()
    assert (period.start_date, period.end_date) == (FEB_1, date(2027, 2, 28))
    assert (period.length, period.months, period.weeks, period.term) == ("months", 1, None, None)
    assert period.length_label == "1 month" and period.week_count == 5


def test_a_month_period_refuses_more_than_three_months(as_user, supervisor, location, dept):
    response = create(
        as_user(supervisor), department=dept.pk, length="months", start_date="2027-02-01", months=4
    )
    assert response.status_code == 200 and not SchedulePeriod.objects.exists()
    assert "Ensure this value is less than or equal to 3" in response.content.decode()


def test_a_term_period_uses_the_terms_dates(as_user, supervisor, location, dept, term):
    create(as_user(supervisor), department=dept.pk, length="term", term=term.pk, start_date="2027-03-03")
    period = SchedulePeriod.objects.get()
    assert (period.start_date, period.end_date, period.term) == (term.start_date, term.end_date, term)
    assert period.length_label == "Spring 2027 term"
    assert period.domain().is_part_week(0)  # the term starts on a Tuesday


def test_a_term_period_needs_a_term(as_user, supervisor, location, dept, term):
    response = create(as_user(supervisor), department=dept.pk, length="term")
    assert "Choose the term." in response.content.decode() and not SchedulePeriod.objects.exists()


def test_a_period_that_overlaps_another_is_refused(as_user, supervisor, org, location, dept):
    feb = month(org, location, dept)
    # Weeks from Sat 27 Feb would share 27 and 28 Feb with the month.
    response = create(
        as_user(supervisor), department=dept.pk, length="weeks", start_date="2027-02-27", weeks=2
    )
    assert SchedulePeriod.objects.get() == feb
    assert f"This would overlap the 1 month period {feb}" in response.content.decode()


def test_a_period_right_after_another_is_allowed(as_user, supervisor, org, location, dept):
    month(org, location, dept)
    create(as_user(supervisor), department=dept.pk, length="months", start_date="2027-03-01", months=1)
    assert SchedulePeriod.objects.count() == 2


def test_a_longer_period_with_the_same_start_is_an_overlap_not_the_same_period(
    as_user, supervisor, org, location, dept, term
):
    weeks = SchedulePeriod.objects.create(
        organization=org, location=location, department=dept, start_date=date(2027, 1, 30), weeks=2
    )
    # A term starting that Saturday: same start, different period.
    spring = Term.objects.create(
        organization=org, name="Late Spring", start_date=date(2027, 1, 30), end_date=date(2027, 4, 30)
    )
    response = create(as_user(supervisor), department=dept.pk, length="term", term=spring.pk)
    assert response.status_code == 200 and SchedulePeriod.objects.get() == weeks
    assert "This would overlap the 2 weeks period" in response.content.decode()


def test_the_same_period_again_opens_the_one_already_there(as_user, supervisor, org, location, dept):
    feb = month(org, location, dept)
    response = create(
        as_user(supervisor), department=dept.pk, length="months", start_date="2027-02-01", months=1
    )
    assert response.status_code == 302 and response["Location"] == reverse("period", args=[feb.pk])


def test_without_terms_the_form_says_where_to_add_one(as_user, supervisor, location, dept):
    client = as_user(supervisor)
    page = client.get(reverse("schedule_index")).content.decode()
    assert "A whole term" not in page and 'name="term"' not in page
    assert f'<a href="{reverse("terms")}">add a term under Settings, Terms</a> first' in page
    response = create(client, department=dept.pk, length="term")
    assert "Select a valid choice" in response.content.decode() and not SchedulePeriod.objects.exists()


def test_raise_hours_headings_keep_their_dates_capitalised(
    as_user, supervisor, org, location, dept, term, employee
):
    employee.departments.add(dept)
    period = month(org, location, dept)
    page = as_user(supervisor).get(reverse("raise_hours", args=[period.pk, employee.pk])).content.decode()
    assert "Week 1 (Mon 1 Feb to Fri 5 Feb, part week): Avery has" in page
    assert "Week 2 (Sat 6 Feb): Avery has" in page


def test_editing_the_term_later_never_moves_the_period(as_user, supervisor, location, dept, term):
    create(as_user(supervisor), department=dept.pk, length="term", term=term.pk)
    term.end_date = date(2027, 5, 14)
    term.save()
    assert SchedulePeriod.objects.get().end_date == date(2027, 5, 7)


# --- The grid, reports, versions and print ---------------------------------------------------


def test_the_grid_shows_a_part_week_with_only_its_days(as_user, supervisor, org, location, dept, term):
    period = month(org, location, dept)
    page = as_user(supervisor).get(reverse("period", args=[period.pk])).content.decode()
    assert "Mon 1 Feb" in page and "Fri 5 Feb" in page and "Sat 30 Jan" not in page
    assert "(part week: only the days in this period)" in page
    assert 'href="?week=4"' in page and "27 Feb, part" in page
    last = as_user(supervisor).get(reverse("period", args=[period.pk]) + "?week=4").content.decode()
    assert "Sat 27 Feb" in last and "Sun 28 Feb" in last and "Mon 1 Mar" not in last


def test_a_whole_week_period_has_no_part_weeks(as_user, supervisor, org, location, dept, term):
    period = SchedulePeriod.objects.create(
        organization=org, location=location, department=dept, start_date=date(2027, 1, 16), weeks=2
    )
    page = as_user(supervisor).get(reverse("period", args=[period.pk])).content.decode()
    assert "part week" not in page and ", part" not in page


def test_reports_label_part_weeks_and_json_lists_the_weeks(
    as_user, supervisor, org, location, dept, term, employee
):
    period = month(org, location, dept)
    add_shift(period, employee, date(2027, 2, 27), 720, 960)
    client = as_user(supervisor)
    page = client.get(reverse("reports", args=[period.pk])).content.decode()
    assert "Week 5" in page and "27 Feb, part" in page
    data = json.loads(client.get(reverse("reports", args=[period.pk]) + "?report=hours&format=json").content)
    assert [w["days"] for w in data["period"]["weeks"]] == [5, 7, 7, 7, 2]
    avery = next(r for r in data["rows"] if r["name"] == "Avery")
    assert avery["weeks"] == [0.0, 0.0, 0.0, 0.0, 4.0]


def test_version_print_splits_the_month_into_calendar_weeks(
    as_user, supervisor, org, location, dept, term, employee
):
    period = month(org, location, dept)
    add_shift(period, employee, FEB_1)
    add_shift(period, employee, date(2027, 2, 27), 720, 960)
    version = services.publish(period, supervisor)
    page = (
        as_user(supervisor).get(reverse("version_print", args=[period.pk, version.number])).content.decode()
    )
    assert "Week 1: Mon 1 Feb to Fri 5 Feb 2027 (part week)" in page
    assert "Week 2: Sat 6 Feb to Fri 12 Feb 2027</caption>" in page
    assert "Week 5: Sat 27 Feb to Sun 28 Feb 2027 (part week)" in page


# --- Caps across neighbouring periods (whole calendar weeks) ---------------------------------


def _feb_with_january_weekend(org, location, dept, employee, sunday: bool):
    january = month(org, location, dept, start=date(2027, 1, 1))
    add_shift(january, employee, date(2027, 1, 30), 720, 960)  # Sat 12-4
    if sunday:
        add_shift(january, employee, date(2027, 1, 31), 1080, 1320)  # Sun 6-10pm
    february = month(org, location, dept)
    for d in (1, 2, 3, 4):
        add_shift(february, employee, date(2027, 2, d))  # Mon-Thu 10-2: 16h
    return february


def test_weekly_cap_counts_the_previous_months_weekend(org, location, dept, term, employee):
    february = _feb_with_january_weekend(org, location, dept, employee, sunday=True)
    caps = [i for i in services.validate_period(february).issues if i.category == Category.CAP]
    assert len(caps) == 1 and "24h in the week of Sat 30 Jan" in caps[0].message  # default cap 20h


def test_weekly_cap_passes_when_the_whole_week_is_within_it(org, location, dept, term, employee):
    february = _feb_with_january_weekend(org, location, dept, employee, sunday=False)
    assert [i for i in services.validate_period(february).issues if i.category == Category.CAP] == []


# --- Published shifts late in a long period --------------------------------------------------


def test_my_schedule_finds_shifts_late_in_a_term_period(org, location, dept, term, employee, supervisor):
    period = SchedulePeriod.objects.create(
        organization=org,
        location=location,
        department=dept,
        length=PeriodLength.TERM,
        start_date=term.start_date,
        term=term,
    )
    add_shift(period, employee, date(2027, 5, 3))
    services.publish(period, supervisor)
    found = my_upcoming(employee, date(2027, 5, 1))  # 16 weeks after the period starts
    assert [m.shift.day for m in found] == [date(2027, 5, 3)]
    assert my_upcoming(employee, date(2027, 5, 4)) == []


def test_the_period_page_says_when_it_runs_past_its_term(as_user, supervisor, org, location, dept, term):
    period = month(org, location, dept, start=date(2027, 5, 1))
    page = as_user(supervisor).get(reverse("period", args=[period.pk])).content.decode()
    assert "This period runs past the end of Spring 2027 (Fri 7 May)" in page
    inside = month(org, location, dept, start=date(2027, 3, 1))
    page = as_user(supervisor).get(reverse("period", args=[inside.pk])).content.decode()
    assert "runs past the end" not in page


# --- Solves on long periods take a span of at most 31 days -----------------------------------


def _solve_form(period, **data):
    base = {"mode": "rebalance", "change_limit_percent": 30, "freeze_hours": 48}
    return SolveForm({**base, **data}, period=period)


def test_a_solve_on_a_term_period_needs_a_span_of_at_most_31_days(org, location, dept, term):
    period = SchedulePeriod.objects.create(
        organization=org,
        location=location,
        department=dept,
        length=PeriodLength.TERM,
        start_date=term.start_date,
        term=term,
    )
    assert "first_day" in _solve_form(period).errors
    too_long = _solve_form(period, first_day="2027-02-01", last_day="2027-03-05")
    assert too_long.errors["last_day"] == ["Choose at most 31 days."]
    assert _solve_form(period, first_day="2027-02-01", last_day="2027-03-03").is_valid()


def test_a_whole_month_solves_in_one_run(org, location, dept, term):
    period = month(org, location, dept, start=date(2027, 3, 1))  # 31 days
    assert _solve_form(period).is_valid()


def test_the_solve_page_suggests_four_weeks_for_a_term(as_user, supervisor, org, location, dept, term):
    period = SchedulePeriod.objects.create(
        organization=org,
        location=location,
        department=dept,
        length=PeriodLength.TERM,
        start_date=term.start_date,
        term=term,
    )
    page = as_user(supervisor).get(reverse("solve_start", args=[period.pk])).content.decode()
    assert "This period is longer than 31 days" in page
    assert 'name="first_day"' in page and "required" in page
