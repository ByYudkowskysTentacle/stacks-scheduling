# SPDX-License-Identifier: AGPL-3.0-or-later
"""The student view of the published schedule (D6, SPEC §9.2) and the published-version export
for the Homebase fallback (D47)."""

from __future__ import annotations

import csv
import io
import re
from datetime import date
from decimal import Decimal
from unittest import mock

import pytest
from django.urls import reverse

from app.core.models import Term
from app.people.models import Employee, EmployeeTag, Tag
from app.scheduling import services
from app.scheduling.models import SchedulePeriod, Shift
from app.scheduling.published import NEUTRAL_CHIP

pytestmark = pytest.mark.django_db
START = date(2027, 1, 16)
MONDAY = date(2027, 1, 18)


@pytest.fixture
def setup(org, location, supervisor, student_user, employee, pay_role):
    Term.objects.create(
        organization=org, name="Spring 2027", start_date=date(2027, 1, 11), end_date=date(2027, 5, 7)
    )
    blake = Employee.objects.create(
        organization=org,
        full_name="Blake Fullname-Sample",
        preferred_name="Blake",
        email="blake@example.edu",
        default_wage=Decimal("14.25"),
        notes="Private supervisor note",
        default_pay_role=pay_role,
    )
    EmployeeTag.objects.create(
        organization=org, employee=blake, tag=Tag.objects.create(organization=org, name="Cataloguer")
    )
    period = SchedulePeriod.objects.create(organization=org, location=location, start_date=START, weeks=2)

    def shift(who, start, end, note=""):
        return Shift.objects.create(
            organization=org,
            location=location,
            period=period,
            employee=who,
            date=MONDAY,
            start_min=start,
            end_min=end,
            pay_role=pay_role,
            note=note,
        )

    mine = shift(employee, 540, 720, note="Bring your badge")
    shift(blake, 600, 780, note="Blake's private note")
    Shift.objects.create(
        organization=org,
        location=location,
        period=period,
        employee=None,
        date=MONDAY,
        start_min=780,
        end_min=900,
    )
    services.publish(period, supervisor)
    return period, blake, mine


def test_students_see_the_published_week_with_preferred_names_only(as_user, student_user, setup):
    _period, _blake, _ = setup
    page = as_user(student_user).get(reverse("published_schedule") + "?week=2027-01-18").content.decode()
    assert "Blake" in page and "You" in page and "Open shift" in page
    for hidden in (
        "Fullname-Sample",
        "blake@example.edu",
        "14.25",
        "Private supervisor note",
        "Cataloguer",
        "Blake's private note",
    ):
        assert hidden not in page
    assert "Bring your badge" in page  # your own note shows


def test_draft_changes_stay_hidden_until_published(as_user, student_user, setup):
    _, _, mine = setup
    mine.start_min = 600
    mine.save()
    page = as_user(student_user).get(reverse("published_schedule") + "?week=2027-01-18").content.decode()
    assert "9am–12pm" in page and "10am–12pm" not in page


def test_my_schedule_lists_who_else_is_on(as_user, student_user, setup):
    with mock.patch("django.utils.timezone.localdate", return_value=START):
        page = as_user(student_user).get(reverse("my_schedule")).content.decode()
    assert "Mon 18 Jan" in page or "Monday 18 Jan" in page
    assert "With Blake" in page


def test_supervisor_downloads_the_published_version_as_csv(as_user, supervisor, setup):
    period, _blake, _ = setup
    response = as_user(supervisor).get(reverse("version_export", args=[period.pk, 1]))
    assert response.status_code == 200 and response["Content-Type"].startswith("text/csv")
    assert "attachment" in response["Content-Disposition"]
    rows = list(csv.DictReader(io.StringIO(response.content.decode())))
    assert [r["preferred_name"] for r in rows] == ["Avery", "Blake", "Open shift"]
    assert (
        rows[1]["full_name"] == "Blake Fullname-Sample"
        and rows[1]["start"] == "10:00"
        and rows[1]["hours"] == "3"
    )
    assert rows[0]["weekday"] == "Monday" and rows[0]["pay_role"] == "Student Assistant"


def test_only_supervisors_can_export(as_user, student_user, lead_user, setup):
    period, _, _ = setup
    for user in (student_user, lead_user):
        assert as_user(user).get(reverse("version_export", args=[period.pk, 1])).status_code == 403


def test_printable_version_grid(as_user, supervisor, setup):
    period, _, _ = setup
    page = as_user(supervisor).get(reverse("version_print", args=[period.pk, 1])).content.decode()
    assert "Week 1" in page and "Blake" in page and "10am–1pm" in page


def test_home_shows_next_shifts(as_user, student_user, setup):
    with mock.patch("django.utils.timezone.localdate", return_value=START):
        page = as_user(student_user).get(reverse("home")).content.decode()
    assert "Your next shift" in page and "9am–12pm" in page and "Monday" in page


FAR_BEFORE = date(2026, 11, 7)  # ten weeks before the published period starts


def test_home_and_my_schedule_show_shifts_beyond_a_few_weeks(as_user, student_user, setup):
    client = as_user(student_user)
    with mock.patch("django.utils.timezone.localdate", return_value=FAR_BEFORE):
        home = client.get(reverse("home")).content.decode()
        mine = client.get(reverse("my_schedule")).content.decode()
    assert "Mon 18 Jan" in home and "No published shifts coming up" not in home
    assert "18 Jan" in mine and "No published shifts coming up" not in mine


def test_an_empty_published_week_points_to_the_next_week_with_shifts(as_user, student_user, setup):
    page = as_user(student_user).get(reverse("published_schedule") + "?week=2026-11-07").content.decode()
    assert "No published shifts this week." in page
    assert "?week=2027-01-16" in page


def test_a_week_with_shifts_has_no_next_week_hint(as_user, student_user, setup):
    page = as_user(student_user).get(reverse("published_schedule") + "?week=2027-01-18").content.decode()
    assert "No published shifts this week." not in page


def test_pay_roles_show_as_text_on_your_own_shifts_and_stay_private_on_others(
    as_user, student_user, setup, pay_role
):
    """Colour alone isn't enough (WCAG 1.4.1); a co-worker's pay role can reveal work-study
    status, so theirs isn't shown at all (D112)."""
    page = as_user(student_user).get(reverse("published_schedule") + "?week=2027-01-18").content.decode()
    assert page.count(pay_role.name) == 1  # on "You" only, not on Blake's shift
    blake_line = re.search(r'<li class="public-shift"[^>]*>(?:(?!</li>).)*Blake', page, re.S).group(0)
    assert pay_role.color not in blake_line and NEUTRAL_CHIP in blake_line  # Blake's bar is neutral
    with mock.patch("django.utils.timezone.localdate", return_value=START):
        mine = as_user(student_user).get(reverse("my_schedule")).content.decode()
    assert pay_role.name in mine


def test_the_supervisor_grid_names_each_pay_role(as_user, supervisor, setup, pay_role):
    period, _, _ = setup
    page = as_user(supervisor).get(reverse("period", args=[period.pk])).content.decode()
    assert 'aria-label="Pay roles' in page and pay_role.name in page
