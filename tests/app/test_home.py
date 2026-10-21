# SPDX-License-Identifier: AGPL-3.0-or-later
"""Home as a starting point (audit 2.2, 2.3) and the navigation built for each role (D114)."""

from __future__ import annotations

import re
from datetime import date
from unittest import mock

import pytest
from django.urls import reverse

from app.availability.models import TimeOff
from app.scheduling import services
from app.scheduling.models import ExchangeKind, ExchangeStatus, SchedulePeriod, Shift, ShiftExchange

pytestmark = pytest.mark.django_db
START = date(2027, 1, 16)


def home(client) -> str:
    return client.get(reverse("home")).content.decode()


def tabs(html: str) -> list[str]:
    bar = re.search(r'<nav aria-label="Main" class="tab-bar">.*?</nav>', html, re.S).group(0)
    tabs_part = bar.split('class="tab-bar__sheet"')[0]
    return re.findall(r"</svg>([^<]+)</a>", tabs_part)


@pytest.fixture
def published(org, location, supervisor, employee, pay_role):
    period = SchedulePeriod.objects.create(organization=org, location=location, start_date=START, weeks=2)
    shift = Shift.objects.create(
        organization=org,
        location=location,
        period=period,
        employee=employee,
        date=date(2027, 1, 18),
        start_min=540,
        end_min=720,
        pay_role=pay_role,
    )
    services.publish(period, supervisor)
    return period, shift


def test_planner_home_lists_what_needs_attention_with_links(as_user, supervisor, org, employee):
    page = home(as_user(supervisor))
    assert "Nothing is waiting for you" in page
    TimeOff.objects.create(organization=org, employee=employee, start_date=START, end_date=START)
    page = home(as_user(supervisor))
    assert re.search(r"<span class=\"attention-list__count\">1</span> time-off request</a>", page)
    assert reverse("time_off_review") in page


def test_planner_home_shows_current_periods_with_validation(as_user, supervisor, published):
    period, _ = published
    with mock.patch("django.utils.timezone.localdate", return_value=START):
        page = home(as_user(supervisor))
    assert reverse("period", args=[period.pk]) in page and "Version 1" in page


def test_setup_checklist_only_while_setup_is_incomplete(as_user, supervisor, org, location):
    assert "Set up " in home(as_user(supervisor))  # no hours, term or pay roles yet


def test_student_home_says_when_the_next_shift_is(as_user, student_user, published):
    with mock.patch("django.utils.timezone.localdate", return_value=date(2027, 1, 17)):
        page = home(as_user(student_user))
    assert "Tomorrow" in page and "9am–12pm" in page


def test_student_home_flags_a_swap_waiting_for_an_answer(as_user, student_user, org, employee, published):
    _, shift = published
    ShiftExchange.objects.create(
        organization=org,
        kind=ExchangeKind.SWAP,
        status=ExchangeStatus.ASKED,
        shift=shift,
        other_shift=shift,
        requester=employee,
        taker=employee,
    )
    with mock.patch("django.utils.timezone.localdate", return_value=date(2027, 1, 17)):
        page = home(as_user(student_user))
    assert "Needs your answer" in page and "1</span> swap request waiting for you" in page


def test_tab_bar_holds_each_roles_main_destinations(as_user, supervisor, student_user):
    assert tabs(home(as_user(student_user))) == ["Home", "My shifts", "Availability", "Open shifts"]
    assert tabs(home(as_user(supervisor))) == ["Home", "Schedule", "Review", "People"]


def test_review_pages_mark_review_as_the_current_section(as_user, supervisor):
    page = as_user(supervisor).get(reverse("time_off_review")).content.decode()
    nav = re.search(r'<nav aria-label="Main" class="main-nav">.*?</nav>', page, re.S).group(0)
    assert re.search(r'aria-current="page">Review<', nav) and not re.search(
        r'aria-current="page">Schedule<', nav
    )


def test_settings_sub_nav_shows_on_edit_pages_and_marks_the_section(as_user, supervisor, org, pay_role):
    page = as_user(supervisor).get(reverse("pay_role_edit", args=[pay_role.pk])).content.decode()
    nav = re.search(r'<nav aria-label="Settings" class="sub-nav">.*?</nav>', page, re.S).group(0)
    assert re.search(r'aria-current="page">Pay roles<', nav)


def test_forms_have_no_label_colons_and_mark_optional_fields(as_user, supervisor):
    page = as_user(supervisor).get(reverse("schedule_index")).content.decode()
    form = page.split("Start a planning period")[1]
    assert ":</label>" not in form and "---------" not in form


def test_employee_record_shows_the_rules_that_shape_their_hours(as_user, supervisor, org, employee):
    from app.scheduling.models import HourCapRule

    HourCapRule.objects.create(organization=org, scope="week", minutes=600, employee=employee)
    page = as_user(supervisor).get(reverse("employee_detail", args=[employee.pk])).content.decode()
    section = page.split("<h2>Scheduling</h2>")[1]
    assert "Hour caps" in section and "Only the default weekly cap" not in section
