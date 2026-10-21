# SPDX-License-Identifier: AGPL-3.0-or-later
"""The paintable grid writes the text boxes, and only for the day painted."""

from __future__ import annotations

from datetime import date

import pytest

from app.core.models import Term
from tests.conftest import PASSWORD

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def page(browser, live_server, org, location, student_user):
    Term.objects.create(
        organization=org, name="Spring 2027", start_date=date(2027, 1, 11), end_date=date(2027, 5, 7)
    )
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    pg = context.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(live_server.url + "/accounts/login/")
    pg.fill("input[name=login]", student_user.email)
    pg.fill("input[name=password]", PASSWORD)
    pg.click("button[type=submit]")
    pg.goto(live_server.url + "/availability/")
    yield pg
    assert errors == []
    context.close()


def cell(pg, weekday, minute, start=7 * 60):
    return pg.locator(f'td[data-weekday="{weekday}"][data-slot="{(minute - start) // 15}"]')


def test_mouse_drag_paints_unavailable_time(page):
    page.locator("input[name=unavailable_2]").fill("all day")  # Wednesday stays as typed
    page.locator("input[name=unavailable_2]").dispatch_event("change")
    page.locator("input[name=unavailable_0]").fill("")
    cell(page, 0, 9 * 60).hover()
    page.mouse.down()
    cell(page, 0, 9 * 60 + 45).hover()
    page.mouse.up()
    assert page.locator("input[name=unavailable_0]").input_value() == "9am-10am"
    assert page.locator("input[name=unavailable_2]").input_value() == "all day"


def test_preferred_brush_and_text_box_sync(page):
    page.get_by_role("button", name="Prefer").click()
    cell(page, 1, 13 * 60).hover()
    page.mouse.down()
    cell(page, 1, 14 * 60 + 45).hover()
    page.mouse.up()
    assert page.locator("input[name=preferred_1]").input_value() == "1pm-3pm"
    page.locator("input[name=unavailable_1]").fill("13:30-14:00")
    page.locator("input[name=unavailable_1]").dispatch_event("change")
    assert "cell--unavailable" in (cell(page, 1, 13 * 60 + 30).get_attribute("class") or "")


def test_saved_form_round_trips(page):
    cell(page, 4, 12 * 60).hover()
    page.mouse.down()
    cell(page, 4, 12 * 60 + 15).hover()
    page.mouse.up()
    page.get_by_role("button", name="Save availability").first.click()  # the one under the grid
    assert page.locator("input[name=unavailable_4]").input_value() == "12pm-12:30pm"


def test_phone_taps_start_then_end(browser, live_server, org, location, student_user):
    Term.objects.create(
        organization=org, name="Spring 2027", start_date=date(2027, 1, 11), end_date=date(2027, 5, 7)
    )
    context = browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True)
    pg = context.new_page()
    pg.goto(live_server.url + "/accounts/login/")
    pg.fill("input[name=login]", student_user.email)
    pg.fill("input[name=password]", PASSWORD)
    pg.click("button[type=submit]")
    pg.goto(live_server.url + "/availability/")
    cell(pg, 3, 10 * 60).tap()
    cell(pg, 3, 11 * 60 + 15).tap()
    assert pg.locator("input[name=unavailable_3]").input_value() == "10am-11:30am"
    context.close()


def test_phone_scroll_gestures_never_paint(browser, live_server, org, location, student_user):
    """A swipe that starts on the grid (pointer moves, or the browser cancels it to scroll) must
    not count as a tap; only a real tap sets the start, and the status line says what's next."""
    Term.objects.create(
        organization=org, name="Spring 2027", start_date=date(2027, 1, 11), end_date=date(2027, 5, 7)
    )
    context = browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True)
    pg = context.new_page()
    pg.goto(live_server.url + "/accounts/login/")
    pg.fill("input[name=login]", student_user.email)
    pg.fill("input[name=password]", PASSWORD)
    pg.click("button[type=submit]")
    pg.goto(live_server.url + "/availability/")
    touch = {"pointerType": "touch", "pointerId": 7, "bubbles": True, "clientX": 100, "clientY": 300}
    start, end = cell(pg, 3, 10 * 60), cell(pg, 3, 11 * 60 + 15)
    # Two swipes in the same column: one cancelled by the browser, one that moves too far.
    start.dispatch_event("pointerdown", touch)
    start.dispatch_event("pointercancel", touch)
    end.dispatch_event("pointerdown", touch)
    end.dispatch_event("pointermove", {**touch, "clientY": 360})
    end.dispatch_event("pointerup", {**touch, "clientY": 360})
    assert pg.locator("input[name=unavailable_3]").input_value() == ""
    assert pg.locator(".painter__status").inner_text() == ""
    start.tap()
    assert "Start: " in pg.locator(".painter__status").inner_text()
    end.tap()
    assert pg.locator("input[name=unavailable_3]").input_value() == "10am-11:30am"
    assert "Painted " in pg.locator(".painter__status").inner_text()
    context.close()
