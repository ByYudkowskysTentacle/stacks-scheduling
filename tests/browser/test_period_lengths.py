# SPDX-License-Identifier: AGPL-3.0-or-later
"""Month and term periods in a real browser (D119, D120): the new-period form shows only the
fields a length uses, the pages that grew with these features fit a 390px phone, and a month's
published version prints one table per calendar week without clipping (CLAUDE.md §2.7)."""

from __future__ import annotations

from datetime import date

import pytest

from app.core.models import Department, Term
from app.people.models import Employee
from app.scheduling import services
from app.scheduling.models import PeriodLength, SchedulePeriod, Shift
from tests.browser.test_print import LANDSCAPE_A4_CSS_PX
from tests.conftest import PASSWORD, manages

pytestmark = pytest.mark.django_db(transaction=True)


def _signed_in(browser, live_server, user, width):
    context = browser.new_context(viewport={"width": width, "height": 800})
    pg = context.new_page()
    pg.goto(live_server.url + "/accounts/login/")
    pg.fill("input[name=login]", user.email)
    pg.fill("input[name=password]", PASSWORD)
    pg.click("button[type=submit]")
    return context, pg


def _february(org, location, supervisor):
    dept = Department.objects.create(organization=org, name="Circulation")
    manages(supervisor, dept)
    Term.objects.create(
        organization=org, name="Spring 2027", start_date=date(2027, 1, 12), end_date=date(2027, 5, 7)
    )
    period = SchedulePeriod.objects.create(
        organization=org,
        location=location,
        department=dept,
        length=PeriodLength.MONTHS,
        start_date=date(2027, 2, 1),
        months=1,
    )
    for i in range(20):
        emp = Employee.objects.create(
            organization=org, full_name=f"Person {i:02d} Example", preferred_name=f"Person {i:02d} Longername"
        )
        emp.departments.add(dept)
        for d in period.days[i % 3 :: 3]:
            Shift.objects.create(
                organization=org,
                location=location,
                period=period,
                employee=emp,
                date=d,
                start_min=720 + 15 * (i % 4),
                end_min=900 + 15 * (i % 4),
            )
    return period


def test_the_period_form_shows_only_the_fields_a_length_uses(browser, live_server, org, location, supervisor):
    Department.objects.create(organization=org, name="Circulation")
    Term.objects.create(
        organization=org, name="Spring 2027", start_date=date(2027, 1, 12), end_date=date(2027, 5, 7)
    )
    context, pg = _signed_in(browser, live_server, supervisor, 1024)
    pg.goto(live_server.url + "/schedule/")

    def shown():
        return {n: pg.locator(f"[name={n}]").is_visible() for n in ("start_date", "weeks", "months", "term")}

    assert shown() == {"start_date": True, "weeks": True, "months": False, "term": False}
    pg.get_by_label("Calendar months").check()
    assert shown() == {"start_date": True, "weeks": False, "months": True, "term": False}
    pg.get_by_label("A whole term").check()
    assert shown() == {"start_date": False, "weeks": False, "months": False, "term": True}
    context.close()


def test_month_pages_fit_a_phone(browser, live_server, org, location, supervisor):
    period = _february(org, location, supervisor)
    context, pg = _signed_in(browser, live_server, supervisor, 390)
    for path in (
        f"/schedule/{period.pk}/",
        f"/schedule/{period.pk}/?week=4",
        f"/schedule/{period.pk}/reports/",
        "/schedule/",
        "/settings/",
        "/settings/locations/",  # under Organization since D121
        "/settings/departments/",
    ):
        pg.goto(live_server.url + path)
        width = pg.evaluate("() => document.documentElement.scrollWidth")
        assert width <= 390, f"{path} is {width}px wide on a 390px phone"
    context.close()


def test_a_months_version_prints_one_unclipped_table_per_calendar_week(
    browser, live_server, org, location, supervisor
):
    period = _february(org, location, supervisor)
    services.publish(period, supervisor)
    context, pg = _signed_in(browser, live_server, supervisor, LANDSCAPE_A4_CSS_PX)
    pg.goto(live_server.url + f"/schedule/{period.pk}/versions/1/print/")
    assert pg.locator(".schedule-grid").count() == 5  # part, three whole weeks, part
    assert pg.locator(".schedule-grid caption", has_text="(part week)").count() == 2
    pg.emulate_media(media="print")
    assert pg.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 0
    clipped = pg.evaluate(
        """[...document.querySelectorAll('.schedule-grid th, .schedule-grid td')]
             .filter(c => c.scrollWidth > c.clientWidth + 1).length"""
    )
    assert clipped == 0
    context.close()
