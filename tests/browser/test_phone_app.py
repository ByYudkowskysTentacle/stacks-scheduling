# SPDX-License-Identifier: AGPL-3.0-or-later
"""The service worker in a real browser (D124): a page the person has viewed opens without a
network, marked as a saved copy; a page they haven't gets the offline page; and signing out
drops the saved copies (D122)."""

from __future__ import annotations

import pytest

from tests.conftest import PASSWORD

pytestmark = pytest.mark.django_db(transaction=True)
PHONE = {"width": 390, "height": 844}


def _controlled_page(browser, live_server, user):
    context = browser.new_context(viewport=PHONE)
    pg = context.new_page()
    pg.goto(live_server.url + "/accounts/login/")
    pg.fill("input[name=login]", user.email)
    pg.fill("input[name=password]", PASSWORD)
    pg.check("input[name=remember]")
    pg.click("main form button[type=submit]")
    pg.wait_for_url(live_server.url + "/")
    pg.evaluate("navigator.serviceWorker.ready.then(() => true)")
    pg.reload()  # the first page loaded before the worker took over
    assert pg.evaluate("!!navigator.serviceWorker.controller")
    return context, pg


def test_saved_pages_open_offline_and_sign_out_drops_them(browser, live_server, student_user):
    context, pg = _controlled_page(browser, live_server, student_user)
    pg.goto(live_server.url + "/my-schedule/")
    assert pg.locator("[data-offline-note]").is_hidden()

    context.set_offline(True)
    pg.goto(live_server.url + "/my-schedule/")
    assert pg.locator("h1").inner_text() == "My schedule"
    note = pg.locator("[data-offline-note]")
    assert note.is_visible() and "You're offline" in note.inner_text()

    pg.goto(live_server.url + "/time-off/")  # never viewed, and not a saved page
    assert pg.locator("h1").inner_text() == "You're offline"

    context.set_offline(False)
    pg.goto(live_server.url + "/")
    pg.locator(".tab-bar__more summary").click()
    pg.locator(".tab-bar__sheet button[type=submit]").click()
    pg.wait_for_url("**/accounts/login/")
    context.set_offline(True)
    pg.goto(live_server.url + "/my-schedule/")
    assert pg.locator("h1").inner_text() == "You're offline"
    context.close()


def test_the_install_card_offers_help_on_an_iphone(browser, live_server, student_user):
    iphone = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
    )
    context = browser.new_context(viewport=PHONE, user_agent=iphone)
    pg = context.new_page()
    pg.goto(live_server.url + "/accounts/login/")
    pg.fill("input[name=login]", student_user.email)
    pg.fill("input[name=password]", PASSWORD)
    pg.click("main form button[type=submit]")
    pg.wait_for_url(live_server.url + "/")
    card = pg.locator("[data-install-card]")
    assert card.is_visible()
    assert pg.locator("[data-install-for=apple]").is_visible()
    assert pg.locator("[data-install-for=prompt]").is_hidden()
    pg.locator("[data-install-dismiss]").click()
    assert card.is_hidden()
    pg.reload()
    assert pg.locator("[data-install-card]").is_hidden()  # "Not now" sticks on this device
    context.close()
