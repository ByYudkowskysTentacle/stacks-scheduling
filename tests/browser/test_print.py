# SPDX-License-Identifier: AGPL-3.0-or-later
"""Print checks for the availability grid and the published schedule grid (CLAUDE.md §2.7,
§5): each fits a landscape page with no horizontal overflow or clipped cells, and the header
row repeats on each page."""

from __future__ import annotations

from datetime import date

import pytest

from app.availability.models import Availability, AvailabilityBlock
from app.core.models import Term
from app.people.models import Employee
from fixtures import synthetic
from tests.conftest import PASSWORD

pytestmark = pytest.mark.django_db(transaction=True)
LANDSCAPE_A4_CSS_PX = 1047  # 297mm minus 2 x 10mm margins at 96 dpi


def test_availability_grid_prints_without_clipping(browser, live_server, org, location, supervisor):
    import random

    term = Term.objects.create(
        organization=org, name="Spring 2027", start_date=date(2027, 1, 11), end_date=date(2027, 5, 7)
    )
    rng = random.Random(3)  # noqa: S311
    for i in range(30):
        emp = Employee.objects.create(
            organization=org, full_name=f"Person {i:02d} Example", preferred_name=f"Person {i:02d}"
        )
        a = Availability.objects.create(organization=org, employee=emp, term=term)
        for b in synthetic.student_pattern(rng):
            AvailabilityBlock.objects.create(
                organization=org,
                availability=a,
                weekday=b.weekday,
                kind=str(b.kind),
                start_min=b.interval.start,
                end_min=b.interval.end,
            )
    context = browser.new_context(viewport={"width": LANDSCAPE_A4_CSS_PX, "height": 800})
    pg = context.new_page()
    pg.goto(live_server.url + "/accounts/login/")
    pg.fill("input[name=login]", supervisor.email)
    pg.fill("input[name=password]", PASSWORD)
    pg.click("button[type=submit]")
    pg.goto(live_server.url + f"/availability/grid/?term={term.pk}")
    pg.emulate_media(media="print")
    overflow = pg.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0
    clipped = pg.evaluate(
        """[...document.querySelectorAll('.avail-grid th, .avail-grid td')]
             .filter(c => c.scrollWidth > c.clientWidth + 1).length"""
    )
    assert clipped == 0
    assert (
        pg.evaluate("getComputedStyle(document.querySelector('.avail-grid thead')).display")
        == "table-header-group"
    )
    assert pg.evaluate("getComputedStyle(document.querySelector('.site-header')).display") == "none"
    pdf = pg.pdf(landscape=True, format="A4", print_background=True)
    assert pdf[:4] == b"%PDF"
    context.close()


def test_version_grid_prints_without_clipping(browser, live_server, org, location, supervisor):
    """Audit 2.13: the published version's people-by-days grid fits a landscape page."""
    from app.scheduling import services
    from app.scheduling.models import SchedulePeriod, Shift

    period = SchedulePeriod.objects.create(
        organization=org, location=location, start_date=date(2027, 1, 16), weeks=2
    )
    for i in range(30):
        emp = Employee.objects.create(
            organization=org, full_name=f"Person {i:02d} Example", preferred_name=f"Person {i:02d} Longername"
        )
        for d in period.days[i % 3 :: 3]:
            for start in (480, 900):  # two shifts on one day, to fill a cell
                Shift.objects.create(
                    organization=org,
                    location=location,
                    period=period,
                    employee=emp,
                    date=d,
                    start_min=start + 15 * (i % 4),
                    end_min=start + 195 + 15 * (i % 4),
                )
    services.publish(period, supervisor)
    context = browser.new_context(viewport={"width": LANDSCAPE_A4_CSS_PX, "height": 800})
    pg = context.new_page()
    pg.goto(live_server.url + "/accounts/login/")
    pg.fill("input[name=login]", supervisor.email)
    pg.fill("input[name=password]", PASSWORD)
    pg.click("button[type=submit]")
    for path in (f"/schedule/{period.pk}/versions/1/", f"/schedule/{period.pk}/versions/1/print/"):
        pg.goto(live_server.url + path)
        assert pg.locator(".schedule-grid").count() == 2  # one table per week
        pg.emulate_media(media="print")
        assert pg.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 0
        clipped = pg.evaluate(
            """[...document.querySelectorAll('.schedule-grid th, .schedule-grid td')]
                 .filter(c => c.scrollWidth > c.clientWidth + 1).length"""
        )
        assert clipped == 0
        assert (
            pg.evaluate("getComputedStyle(document.querySelector('.schedule-grid thead')).display")
            == "table-header-group"
        )
        pg.emulate_media(media="screen")
    context.close()
