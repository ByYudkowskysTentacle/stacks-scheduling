# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tables that scroll sideways on phones get a keyboard tab stop (WCAG 2.1.1), and lose it
when they fit."""

from __future__ import annotations

import pytest

from app.people.models import Employee
from tests.conftest import PASSWORD

pytestmark = pytest.mark.django_db(transaction=True)


def test_wide_tables_are_focusable_only_while_they_scroll(browser, live_server, org, location, supervisor):
    for i in range(3):
        Employee.objects.create(
            organization=org,
            full_name=f"A Rather Long Full Name Number {i} Example",
            preferred_name=f"Person {i}",
            email=f"person-with-a-long-address-{i}@example.edu",
        )
    context = browser.new_context(viewport={"width": 360, "height": 800})
    pg = context.new_page()
    pg.goto(live_server.url + "/accounts/login/")
    pg.fill("input[name=login]", supervisor.email)
    pg.fill("input[name=password]", PASSWORD)
    pg.click("button[type=submit]")
    pg.goto(live_server.url + "/people/")
    table = pg.locator(".data-table").first
    assert pg.evaluate("(t) => t.scrollWidth > t.clientWidth", table.element_handle())
    assert table.get_attribute("tabindex") == "0"
    pg.set_viewport_size({"width": 1400, "height": 800})
    pg.wait_for_function("() => !document.querySelector('.data-table').hasAttribute('tabindex')")
    context.close()


def test_phone_pages_never_scroll_sideways_as_a_whole(
    browser, live_server, org, location, supervisor, employee
):
    """Visually hidden text inside a scrolling grid must stay inside it (WCAG 1.4.10). Before the
    fix, the period grid's hidden ", edit" labels widened the page to about 1,000px."""
    from datetime import date

    from app.scheduling.models import SchedulePeriod, Shift

    period = SchedulePeriod.objects.create(
        organization=org, location=location, start_date=date(2027, 1, 16), weeks=1
    )
    for day in range(16, 23):
        Shift.objects.create(
            organization=org,
            location=location,
            period=period,
            employee=employee,
            date=date(2027, 1, day),
            start_min=540,
            end_min=720,
        )
    context = browser.new_context(viewport={"width": 390, "height": 800})
    pg = context.new_page()
    pg.goto(live_server.url + "/accounts/login/")
    pg.fill("input[name=login]", supervisor.email)
    pg.fill("input[name=password]", PASSWORD)
    pg.click("button[type=submit]")
    for path in (f"/schedule/{period.pk}/", "/people/"):
        pg.goto(live_server.url + path)
        width = pg.evaluate("() => document.documentElement.scrollWidth")
        assert width <= 390, f"{path} is {width}px wide on a 390px phone"
    context.close()
