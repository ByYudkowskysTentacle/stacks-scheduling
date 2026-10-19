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
