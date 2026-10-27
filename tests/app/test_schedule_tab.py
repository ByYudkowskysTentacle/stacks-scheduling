# SPDX-License-Identifier: AGPL-3.0-or-later
"""One Schedule tab with a Draft | Published switch for planners (D111). Students keep seeing the
published week only, and never a draft (CLAUDE.md §2.2)."""

from __future__ import annotations

import re
from datetime import date

import pytest
from django.urls import reverse

from app.scheduling import services
from app.scheduling.models import SchedulePeriod, Shift

pytestmark = pytest.mark.django_db
START = date(2027, 1, 16)
WEEK_2 = date(2027, 1, 23)


@pytest.fixture
def period(org, location, supervisor, employee, pay_role):
    period = SchedulePeriod.objects.create(organization=org, location=location, start_date=START, weeks=2)
    Shift.objects.create(
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
    return period


def main_nav(html: str) -> list[tuple[str, str, bool]]:
    """(label, href, is current) for each link in the main navigation."""
    nav = re.search(r'<nav aria-label="Main".*?</nav>', html, re.S)
    assert nav, "main navigation missing"
    return [
        (label.strip(), href, "aria-current" in attrs)
        for href, attrs, label in re.findall(r'<a href="([^"]+)"([^>]*)>([^<]+)</a>', nav.group(0))
    ]


def switch(html: str) -> dict[str, tuple[str, bool]] | None:
    """{label: (href, is current)} for the Draft | Published switch, or None when absent."""
    nav = re.search(r'<nav class="sub-nav view-switch[^"]*" aria-label="Schedule view">.*?</nav>', html, re.S)
    if not nav:
        return None
    links = re.findall(r'<a href="([^"]+)"([^>]*)>([^<]+)</a>', nav.group(0))
    return {label: (href.replace("&amp;", "&"), "aria-current" in attrs) for href, attrs, label in links}


def page(client, name, *args, query=""):
    resp = client.get(reverse(name, args=args) + query)
    assert resp.status_code == 200
    return resp.content.decode()


def test_students_get_one_schedule_tab_that_opens_the_published_week(as_user, student_user, period):
    html = page(as_user(student_user), "published_schedule", query="?week=2027-01-18")
    nav = main_nav(html)
    labels = [label for label, _, _ in nav]
    assert labels.count("Schedule") == 1 and "Published" not in labels
    assert ("Schedule", reverse("published_schedule"), True) in nav
    assert switch(html) is None and "mode-banner" not in html
    assert "Changes appear here once a supervisor publishes them." in html


def test_students_still_cannot_open_any_draft_view(as_user, student_user, period):
    client = as_user(student_user)
    for url in (reverse("schedule_index"), reverse("period", args=[period.pk])):
        assert client.get(url).status_code == 403


def test_planners_get_one_schedule_tab_that_opens_the_working_copy(as_user, supervisor, period):
    html = page(as_user(supervisor), "schedule_index")
    nav = main_nav(html)
    labels = [label for label, _, _ in nav]
    assert labels.count("Schedule") == 1 and "Published" not in labels
    assert ("Schedule", reverse("schedule_index"), True) in nav
    assert switch(html) == {
        "Draft": (reverse("schedule_index"), True),
        "Published": (reverse("published_schedule"), False),
    }
    assert "<strong>Draft:</strong>" in html


def test_period_week_switches_to_the_same_published_week(as_user, supervisor, period):
    html = page(as_user(supervisor), "period", period.pk, query="?week=1")
    assert switch(html) == {
        "Draft": (reverse("period", args=[period.pk]) + "?week=1", True),
        "Published": (reverse("published_schedule") + "?week=2027-01-23", False),
    }
    assert "Staff don't see changes here until they're published." in html


def test_published_week_switches_back_to_the_period_that_covers_it(as_user, supervisor, period):
    html = page(as_user(supervisor), "published_schedule", query=f"?week={WEEK_2.isoformat()}")
    assert switch(html) == {
        "Draft": (reverse("period", args=[period.pk]) + "?week=1", False),
        "Published": (reverse("published_schedule") + "?week=2027-01-23", True),
    }
    assert "<strong>Published:</strong> what staff see." in html
    assert ("Schedule", reverse("schedule_index"), True) in main_nav(html)  # still the Schedule tab


def test_draft_side_falls_back_to_the_list_when_no_single_period_covers_the_week(
    as_user, admin_user, org, location, period
):
    client = as_user(admin_user)  # sees every department's periods
    html = page(client, "published_schedule", query="?week=2027-03-06")  # no period that week
    assert switch(html)["Draft"] == (reverse("schedule_index"), False)
    from app.core.models import Department

    other = Department.objects.create(organization=org, name="Special Collections")
    SchedulePeriod.objects.create(
        organization=org, location=location, department=other, start_date=START, weeks=2
    )
    html = page(client, "published_schedule", query="?week=2027-01-16")  # two periods cover it
    assert switch(html)["Draft"] == (reverse("schedule_index"), False)


def test_a_week_split_between_two_months_switches_to_the_month_holding_its_first_day(
    as_user, supervisor, org, location
):
    """D120: Sat 30 Jan to Fri 5 Feb is in both the January and February periods."""
    january = SchedulePeriod.objects.create(
        organization=org, location=location, length="months", start_date=date(2027, 1, 1), months=1
    )
    SchedulePeriod.objects.create(
        organization=org, location=location, length="months", start_date=date(2027, 2, 1), months=1
    )
    html = page(as_user(supervisor), "published_schedule", query="?week=2027-01-30")
    # Fri 1 Jan is January's first (part) week, so Sat 30 Jan starts its sixth.
    assert switch(html)["Draft"] == (reverse("period", args=[january.pk]) + "?week=5", False)


def test_shift_leads_get_the_switch_too(as_user, lead_user, period):
    html = page(as_user(lead_user), "published_schedule", query="?week=2027-01-16")
    assert switch(html) is not None
    assert [label for label, _, _ in main_nav(html)].count("Schedule") == 1


def test_switch_pages_share_one_width_so_the_switch_does_not_jump(as_user, supervisor, period):
    client = as_user(supervisor)
    for html in (
        page(client, "schedule_index"),
        page(client, "period", period.pk),
        page(client, "published_schedule", query="?week=2027-01-16"),
    ):
        assert re.search(r'<main[^>]*class="[^"]*main--wide', html)


def test_day_page_and_grid_footer_report_unmet_skill_rules(as_user, supervisor, org, period):
    """A head-count "OK" must not hide an unmet skill rule (CLAUDE.md §2.4)."""
    from app.people.models import Tag
    from app.scheduling.models import TagCoverageRule

    lead = Tag.objects.create(organization=org, name="Shift lead")
    TagCoverageRule.objects.create(organization=org, tag=lead, kind="min_on_duty", count=1)
    client = as_user(supervisor)
    day = page(client, "period_day", period.pk, "2027-01-18")
    assert "Rules not met on this day" in day
    assert "Missing: " in day and "Shift lead" in day
    grid = page(client, "period", period.pk)
    assert re.search(r"\d+ skill or duty rules? not met", grid)


def test_day_page_says_ok_when_rules_are_met(as_user, supervisor, period):
    day = page(as_user(supervisor), "period_day", period.pk, "2027-01-18")
    assert "Rules not met on this day" not in day and "Missing: " not in day


def test_saving_a_shift_reports_what_it_changed_inside_the_validation_card(as_user, supervisor, period):
    client = as_user(supervisor)
    page(client, "period", period.pk)  # remembers the current issue counts
    shift = period.shifts.live().first()
    resp = client.post(
        reverse("shift_edit", args=[period.pk, shift.pk]),
        {
            "employee": shift.employee_id,
            "date": "2027-01-18",
            "start_min": "9am",
            "end_min": "12pm",
            "pay_role": shift.pay_role_id,
        },
        follow=True,
    )
    html = resp.content.decode()
    card = html.split('id="validation"')[1].split("</section>")[0]
    assert "Validation:" in card and re.search(r"\((no change|[+−]\d)", card)


def test_raise_hours_only_where_it_can_work(as_user, supervisor, period, employee):
    employee.in_scheduling_pool = False
    employee.save()
    html = page(as_user(supervisor), "period", period.pk)
    assert "Raise hours" not in html
