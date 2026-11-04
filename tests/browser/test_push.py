# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phone notifications in a real browser (D125). The browser's push subscription is stubbed in
the page (headless Chromium has no push service to reach), and pushes go to a fake sender, so
nothing leaves the test. The service worker's own push handler gets a message through the
DevTools protocol, as a push service would deliver it."""

from __future__ import annotations

import json

import pytest

from adapters import webpush
from adapters.webpush import FakePushSender
from app.scheduling import push
from app.scheduling.models import PushSubscription
from tests.conftest import PASSWORD

pytestmark = pytest.mark.django_db(transaction=True)
PHONE = {"width": 390, "height": 844}
ENDPOINT = "https://fcm.googleapis.com/fcm/send/browser-test-device"
P256DH = "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
AUTH = "BTBZMqHH6r4Tts7J_aSIgg"  # gitleaks:allow (the public RFC 8291 example, not a secret)

# A stand-in for the browser's PushManager, kept in localStorage so it survives a reload. The
# headless shell also reports Notification.permission as "denied" even once it's granted (the
# Permissions API says "granted"), so the page sees the granted state a real browser would.
FAKE_PUSH_MANAGER = """
(() => {
  const KEY = "test-fake-push-subscription";
  Object.defineProperty(Notification, "permission", { configurable: true, get: () => "granted" });
  const make = (data) => data && {
    endpoint: data.endpoint,
    options: { applicationServerKey: Uint8Array.from(data.key).buffer },
    toJSON: () => ({ endpoint: data.endpoint, expirationTime: null, keys: { p256dh: data.p256dh, auth: data.auth } }),
    unsubscribe: () => { localStorage.removeItem(KEY); return Promise.resolve(true); },
  };
  PushManager.prototype.getSubscription = function () {
    return Promise.resolve(make(JSON.parse(localStorage.getItem(KEY) || "null")));
  };
  PushManager.prototype.subscribe = function (options) {
    const data = { ...DEVICE, key: Array.from(new Uint8Array(options.applicationServerKey)) };
    localStorage.setItem(KEY, JSON.stringify(data));
    return Promise.resolve(make(data));
  };
})();
""".replace("DEVICE", json.dumps({"endpoint": ENDPOINT, "p256dh": P256DH, "auth": AUTH}))


@pytest.fixture
def fake(settings, monkeypatch):
    settings.STACKS_VAPID_PRIVATE_KEY = webpush.generate_vapid_key()
    settings.STACKS_VAPID_SUBJECT = "mailto:it@library.example.edu"
    sender = FakePushSender()
    monkeypatch.setattr(push, "get_sender", lambda: sender)
    return sender


def signed_in(browser, live_server, user, **context_options):
    context = browser.new_context(viewport=PHONE, **context_options)
    pg = context.new_page()
    pg.goto(live_server.url + "/accounts/login/")
    pg.fill("input[name=login]", user.email)
    pg.fill("input[name=password]", PASSWORD)
    pg.click("main form button[type=submit]")
    pg.wait_for_url(live_server.url + "/")
    return context, pg


def test_turning_on_sending_a_test_and_turning_off(browser, live_server, student_user, fake):
    context, pg = signed_in(browser, live_server, student_user)
    context.grant_permissions(["notifications"], origin=live_server.url)
    pg.add_init_script(FAKE_PUSH_MANAGER)
    pg.goto(live_server.url + "/notifications/email/")
    on = pg.get_by_role("button", name="Turn on notifications on this device")
    on.wait_for()
    on.click()
    pg.get_by_text("On for this device.", exact=True).wait_for()
    sub = PushSubscription.objects.get()
    assert (sub.user, sub.endpoint, sub.device) == (student_user, ENDPOINT, "Chrome on Linux")

    pg.get_by_role("button", name="Send a test notification").click()
    pg.get_by_text("Test notification sent to 1 device").wait_for()
    ((sent, message),) = fake.sent
    assert sent.endpoint == ENDPOINT and json.loads(message.payload)["title"] == "Test notification"

    pg.get_by_role("button", name="Turn off on this device").click()
    pg.get_by_role("button", name="Turn on notifications on this device").wait_for()
    assert not PushSubscription.objects.exists()
    context.close()


def test_safari_on_an_iphone_is_sent_to_the_install_steps(browser, live_server, student_user, fake):
    iphone = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
    )
    context, pg = signed_in(browser, live_server, student_user, user_agent=iphone)
    pg.add_init_script("delete window.PushManager;")  # Safari outside the Home Screen app has none
    pg.goto(live_server.url + "/notifications/email/")
    pg.get_by_text("notifications work in the Home Screen app").wait_for()
    assert pg.get_by_role("button", name="Turn on notifications on this device").is_hidden()
    context.close()


def test_a_device_someone_else_turned_on_is_turned_off_at_sign_in(
    browser, live_server, student_user, supervisor, fake
):
    PushSubscription.objects.create(user=supervisor, endpoint=ENDPOINT, p256dh=P256DH, auth=AUTH)
    context = browser.new_context(viewport=PHONE)
    context.add_init_script(FAKE_PUSH_MANAGER)
    pg = context.new_page()
    pg.goto(live_server.url + "/accounts/login/")
    # The supervisor's subscription is still in this browser.
    pg.evaluate(
        "device => localStorage.setItem('test-fake-push-subscription', JSON.stringify(device))",
        {
            "endpoint": ENDPOINT,
            "p256dh": P256DH,
            "auth": AUTH,
            "key": list(webpush.b64url_decode(push.public_key())),
        },
    )
    pg.fill("input[name=login]", student_user.email)
    pg.fill("input[name=password]", PASSWORD)
    pg.click("main form button[type=submit]")
    pg.wait_for_url(live_server.url + "/")
    pg.wait_for_function("localStorage.getItem('test-fake-push-subscription') === null")
    assert not PushSubscription.objects.exists()
    context.close()


def test_the_service_worker_shows_each_pushed_notification(browser, live_server, student_user, fake):
    """A real push event, delivered through DevTools as a push service would. The headless shell
    can't display notifications at all, so the worker's showNotification is recorded instead."""
    context, pg = signed_in(browser, live_server, student_user)
    pg.evaluate("navigator.serviceWorker.ready.then(() => true)")
    (worker,) = context.service_workers
    worker.evaluate(
        "self.shown = []; self.registration.showNotification = (title, options) =>"
        " { self.shown.push([title, options]); return Promise.resolve(); }"
    )
    cdp = context.new_cdp_session(pg)
    registrations = []
    cdp.on("ServiceWorker.workerRegistrationUpdated", lambda e: registrations.extend(e["registrations"]))
    cdp.send("ServiceWorker.enable")
    for _ in range(50):
        if registrations:
            break
        pg.wait_for_timeout(100)
    registration_id = registrations[0]["registrationId"]

    def deliver(data: str) -> list:
        before = worker.evaluate("self.shown.length")
        cdp.send(
            "ServiceWorker.deliverPushMessage",
            {"origin": live_server.url, "registrationId": registration_id, "data": data},
        )
        for _ in range(50):
            shown = worker.evaluate("self.shown")
            if len(shown) > before:
                return shown[-1]
            pg.wait_for_timeout(100)
        raise AssertionError("the push showed no notification")

    payload = {
        "title": "Time off approved",
        "body": "Your request was approved.",
        "url": "/time-off/",
        "tag": "stacks-7",
    }
    title, options = deliver(json.dumps(payload))
    assert title == "Time off approved"
    assert options["body"] == "Your request was approved." and options["tag"] == "stacks-7"
    assert options["data"] == {"url": "/time-off/"}
    assert options["icon"].endswith("icon-192.png") and options["badge"].endswith("icon-badge-96.png")

    # A push that isn't ours to read still shows something: Safari stops pushes that show nothing.
    title, options = deliver("not json")
    assert title == "Stacks" and options["data"] == {"url": "/"} and "tag" not in options
    context.close()
