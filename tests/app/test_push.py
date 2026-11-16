# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phone notifications by web push (D125): turning them on per device, which kinds go to the
phone, what a push carries, and what happens when a push service answers. Pushes go to
`FakePushSender`; nothing reaches a real push service. All data is synthetic."""

from __future__ import annotations

import json
from datetime import date

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from adapters import webpush
from adapters.webpush import FakePushSender, Outcome, Result
from app.availability import time_off
from app.availability.models import TimeOff
from app.scheduling import push
from app.scheduling.models import Notification, NotificationCategory, PushPreference, PushSubscription
from app.scheduling.notify import notify
from tests.conftest import make_user

pytestmark = pytest.mark.django_db
MONDAY = date(2027, 1, 18)
P256DH = "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
AUTH = "BTBZMqHH6r4Tts7J_aSIgg"  # gitleaks:allow (the public RFC 8291 example, not a secret)
APPLE = "https://web.push.apple.com/QGuQyavXutnMH-device-one"
GOOGLE = "https://fcm.googleapis.com/fcm/send/device-two"
IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
)


@pytest.fixture
def fake(settings, monkeypatch):
    """Push turned on for the site, with a fake push service."""
    settings.STACKS_VAPID_PRIVATE_KEY = webpush.generate_vapid_key()
    settings.STACKS_VAPID_SUBJECT = "mailto:it@library.example.edu"
    sender = FakePushSender()
    monkeypatch.setattr(push, "get_sender", lambda: sender)
    return sender


def device(user, endpoint=APPLE, **kwargs):
    """A device the person turned on (so confirmed), unless the test says otherwise."""
    fields = {
        "user": user,
        "endpoint": endpoint,
        "p256dh": P256DH,
        "auth": AUTH,
        "confirmed_at": timezone.now(),
    }
    return PushSubscription.objects.create(**{**fields, **kwargs})


def sent_payloads(fake):
    return [(sub.endpoint, json.loads(msg.payload)) for sub, msg in fake.sent]


def post_json(client, name, data, **extra):
    return client.post(reverse(name), json.dumps(data), content_type="application/json", **extra)


def subscription_json(endpoint=APPLE, p256dh=P256DH, auth=AUTH):
    return {"endpoint": endpoint, "expirationTime": None, "keys": {"p256dh": p256dh, "auth": auth}}


# --- Off until the site is set up ---


def test_push_is_off_without_a_vapid_key(settings, as_user, student_user):
    settings.STACKS_VAPID_PRIVATE_KEY = ""
    assert not push.enabled()
    page = as_user(student_user).get(reverse("email_settings")).content.decode()
    assert "Phone notifications" not in page and "push-published" not in page
    assert post_json(as_user(student_user), "push_subscribe", subscription_json()).status_code == 404


def test_push_stays_off_with_a_contact_apple_refuses(settings):
    settings.STACKS_VAPID_PRIVATE_KEY = webpush.generate_vapid_key()
    settings.STACKS_VAPID_SUBJECT = "mailto:stacks@localhost"
    assert not push.enabled()


def test_a_broken_vapid_key_keeps_push_off(settings):
    settings.STACKS_VAPID_PRIVATE_KEY = "not-a-key"
    settings.STACKS_VAPID_SUBJECT = "mailto:it@library.example.edu"
    assert not push.enabled()


# --- Settings page ---


def test_every_kind_goes_to_the_phone_until_turned_off(fake, org, student_user):
    assert push.push_settings(student_user, org) == {c.value: True for c in NotificationCategory}


def test_the_settings_page_saves_email_and_phone_choices_separately(fake, as_user, org, student_user):
    client = as_user(student_user)
    page = client.get(reverse("email_settings")).content.decode()
    assert "Notification settings" in page and "Phone notifications" in page
    assert 'name="push-time_off" value="on" checked' in page
    data = {"published": "on", "push-form": "1", "push-exchange": "on"}
    resp = client.post(reverse("email_settings"), data, follow=True)
    assert "Notification settings saved." in resp.content.decode()
    assert push.push_settings(student_user, org) == {
        "published": False,
        "time_off": False,
        "exchange": True,
        "reminder": False,
    }
    assert PushPreference.objects.filter(user=student_user).count() == 4


def test_a_form_without_the_phone_choices_leaves_them_alone(fake, as_user, org, student_user):
    # Loaded before IT turned push on, so it had no phone checkboxes to send.
    as_user(student_user).post(reverse("email_settings"), {"published": "on"})
    assert push.push_settings(student_user, org) == {c.value: True for c in NotificationCategory}
    assert not PushPreference.objects.exists()


def test_the_settings_page_gives_the_script_what_it_needs(fake, settings, as_user, student_user):
    page = as_user(student_user).get(reverse("email_settings")).content.decode()
    public = webpush.public_key_text(webpush.private_key_from_text(settings.STACKS_VAPID_PRIVATE_KEY))
    assert f'data-public-key="{public}"' in page
    assert reverse("push_subscribe") in page and reverse("push_drop") in page
    assert "js/push.js" in page


def test_the_settings_page_lists_only_the_persons_own_devices(fake, as_user, student_user, supervisor):
    device(student_user, device="Safari on iPhone")
    device(supervisor, GOOGLE, device="Chrome on Android")
    page = as_user(student_user).get(reverse("email_settings")).content.decode()
    assert "Safari on iPhone" in page and "Chrome on Android" not in page


# --- Turning it on for a device ---


def test_turning_on_stores_the_device_for_the_signed_in_person(fake, as_user, student_user):
    client = as_user(student_user)
    resp = post_json(client, "push_subscribe", subscription_json(), HTTP_USER_AGENT=IPHONE_UA)
    assert resp.status_code == 201
    sub = PushSubscription.objects.get()
    assert (sub.user, sub.endpoint, sub.p256dh, sub.auth) == (student_user, APPLE, P256DH, AUTH)
    assert sub.device == "Safari on iPhone"


def test_turning_on_needs_the_csrf_token(fake, student_user):
    client = Client(enforce_csrf_checks=True)
    client.force_login(student_user)
    assert post_json(client, "push_subscribe", subscription_json()).status_code == 403
    assert not PushSubscription.objects.exists()


def test_turning_on_needs_sign_in(fake, client):
    assert post_json(client, "push_subscribe", subscription_json()).status_code == 302
    assert not PushSubscription.objects.exists()


@pytest.mark.parametrize(
    "data",
    [
        subscription_json(endpoint="https://internal.library.example.edu/hook"),
        subscription_json(endpoint="http://fcm.googleapis.com/fcm/send/x"),
        subscription_json(p256dh=P256DH[:-4]),
        subscription_json(auth="short"),
        {"endpoint": APPLE},
        {"endpoint": APPLE + "x" * 2000, "keys": {"p256dh": P256DH, "auth": AUTH}},
        ["not", "an", "object"],
    ],
)
def test_a_bad_subscription_is_refused(fake, as_user, student_user, data):
    assert post_json(as_user(student_user), "push_subscribe", data).status_code == 400
    assert not PushSubscription.objects.exists()


def test_a_device_moves_to_whoever_turns_it_on_next(fake, as_user, student_user, supervisor):
    device(supervisor)
    post_json(as_user(student_user), "push_subscribe", subscription_json())
    assert list(PushSubscription.objects.values_list("user", flat=True)) == [student_user.pk]


def test_the_least_recently_used_device_goes_first_whatever_order_they_were_turned_on(
    fake, as_user, student_user
):
    now = timezone.now()
    # Turned on first, but used every day.
    device(student_user, f"{GOOGLE}-phone", confirmed_at=now - timezone.timedelta(days=300), last_seen_at=now)
    for i in range(9):  # turned on later, unused for months
        device(
            student_user,
            f"{GOOGLE}-{i}",
            confirmed_at=now - timezone.timedelta(days=200 - i),
            last_seen_at=now - timezone.timedelta(days=100 - i),
        )
    post_json(as_user(student_user), "push_subscribe", subscription_json())
    endpoints = set(PushSubscription.objects.values_list("endpoint", flat=True))
    assert f"{GOOGLE}-phone" in endpoints and f"{GOOGLE}-0" not in endpoints and len(endpoints) == 10


def test_a_device_awaiting_its_owner_goes_before_a_confirmed_one(fake, as_user, student_user):
    now = timezone.now()
    device(student_user, f"{GOOGLE}-renewed", confirmed_at=None, last_seen_at=now)
    for i in range(9):
        device(student_user, f"{GOOGLE}-{i}", last_seen_at=now - timezone.timedelta(days=30))
    post_json(as_user(student_user), "push_subscribe", subscription_json())
    assert not PushSubscription.objects.filter(endpoint=f"{GOOGLE}-renewed").exists()


def test_a_person_keeps_at_most_ten_devices(fake, as_user, student_user):
    for i in range(10):
        device(student_user, f"{GOOGLE}-{i}", last_seen_at=timezone.now() - timezone.timedelta(days=20 - i))
    post_json(as_user(student_user), "push_subscribe", subscription_json())
    endpoints = set(PushSubscription.objects.values_list("endpoint", flat=True))
    assert len(endpoints) == 10 and APPLE in endpoints and f"{GOOGLE}-0" not in endpoints


# --- The same phone, someone else signed in ---


def test_a_check_keeps_the_persons_own_device(fake, as_user, student_user):
    sub = device(student_user)
    resp = post_json(as_user(student_user), "push_check", {"endpoint": APPLE, "auth": AUTH})
    assert resp.json() == {"state": "on"}
    sub.refresh_from_db()
    assert sub.last_seen_at is not None


def test_a_check_by_someone_else_drops_the_previous_persons_device(fake, as_user, student_user, supervisor):
    device(supervisor)
    resp = post_json(as_user(student_user), "push_check", {"endpoint": APPLE, "auth": AUTH})
    assert resp.json() == {"state": "off"}
    assert not PushSubscription.objects.exists()


def test_a_check_needs_the_devices_secret_to_drop_anything(fake, as_user, student_user, supervisor):
    device(supervisor)
    resp = post_json(as_user(student_user), "push_check", {"endpoint": APPLE, "auth": "wrong-secret-xx"})
    assert resp.json() == {"state": "off"}
    assert PushSubscription.objects.filter(user=supervisor).exists()


# --- Turning it off: from the device (also on sign-out), or from the list ---


def test_dropping_needs_the_endpoint_and_its_secret_but_no_session(fake, student_user):
    device(student_user)
    client = Client(enforce_csrf_checks=True)  # the service worker has no CSRF token at sign-out
    assert post_json(client, "push_drop", {"endpoint": APPLE, "auth": "wrong-secret-xx"}).status_code == 204
    assert PushSubscription.objects.exists()
    assert post_json(client, "push_drop", {"endpoint": APPLE, "auth": AUTH}).status_code == 204
    assert not PushSubscription.objects.exists()


def test_dropping_takes_only_post(fake, client):
    assert client.get(reverse("push_drop")).status_code == 405


def test_removing_a_device_from_the_list(fake, as_user, student_user, supervisor):
    mine, theirs = device(student_user), device(supervisor, GOOGLE)
    client = as_user(student_user)
    assert client.post(reverse("push_device_remove", args=[theirs.pk])).status_code == 404
    resp = client.post(reverse("push_device_remove", args=[mine.pk]))
    assert resp.status_code == 302
    assert list(PushSubscription.objects.all()) == [theirs]


def renew_to_google(client=None):
    data = {"old": {"endpoint": APPLE, "auth": AUTH}, "subscription": subscription_json(GOOGLE)}
    return post_json(client or Client(enforce_csrf_checks=True), "push_renew", data)


def test_a_renewed_subscription_replaces_the_old_one_but_waits_for_its_owner(
    fake, org, student_user, django_capture_on_commit_callbacks
):
    device(student_user)
    assert renew_to_google().status_code == 204
    sub = PushSubscription.objects.get()
    assert (sub.user, sub.endpoint, sub.confirmed_at) == (student_user, GOOGLE, None)
    # Anyone with the browser could have sent that renewal: nothing goes to it yet.
    with django_capture_on_commit_callbacks(execute=True):
        notify(student_user, org, NotificationCategory.PUBLISHED, "Published", "Your schedule changed")
    assert fake.sent == []


def test_the_owners_next_visit_confirms_a_renewed_device(
    fake, as_user, org, student_user, django_capture_on_commit_callbacks
):
    device(student_user)
    renew_to_google()
    client = as_user(student_user)
    assert "data-push-pending" in client.get(reverse("home")).content.decode()
    assert post_json(client, "push_check", {"endpoint": GOOGLE, "auth": AUTH}).json() == {"state": "on"}
    assert PushSubscription.objects.get().confirmed_at is not None
    assert "data-push-pending" not in client.get(reverse("home")).content.decode()
    with django_capture_on_commit_callbacks(execute=True):
        notify(student_user, org, NotificationCategory.PUBLISHED, "Published", "Your schedule changed")
    assert [e for e, _ in sent_payloads(fake)] == [GOOGLE]


def test_someone_elses_visit_drops_a_renewed_device(fake, as_user, student_user, supervisor):
    device(student_user)
    renew_to_google()
    resp = post_json(as_user(supervisor), "push_check", {"endpoint": GOOGLE, "auth": AUTH})
    assert resp.json() == {"state": "off"} and not PushSubscription.objects.exists()


def test_a_renewed_device_gets_no_test_notification(fake, as_user, student_user):
    device(student_user)
    renew_to_google()
    resp = as_user(student_user).post(reverse("push_test"), follow=True)
    assert "No devices have notifications turned on yet." in resp.content.decode()
    assert fake.sent == []


@pytest.mark.parametrize(
    ("old", "new", "status"),
    [
        ({"endpoint": APPLE, "auth": "wrong-secret-xx"}, subscription_json(GOOGLE), 404),
        ({"endpoint": APPLE, "auth": AUTH}, subscription_json("https://evil.example/x"), 400),
    ],
)
def test_a_renewal_needs_the_old_secret_and_a_known_push_service(fake, student_user, old, new, status):
    device(student_user)
    assert post_json(Client(), "push_renew", {"old": old, "subscription": new}).status_code == status
    assert PushSubscription.objects.get().endpoint == APPLE


# --- What gets pushed ---


def test_a_notification_is_pushed_to_each_of_the_persons_devices(
    fake, org, student_user, django_capture_on_commit_callbacks
):
    device(student_user)
    device(student_user, GOOGLE)
    with django_capture_on_commit_callbacks(execute=True):
        note = notify(
            student_user,
            org,
            NotificationCategory.TIME_OFF,
            "Stacks: time off approved",
            "Hi Avery,\n\nYour time-off request for Mon 18 Jan was approved.\n\nhttps://stacks.example/time-off/\n",
            "/time-off/",
        )
    payloads = sent_payloads(fake)
    assert {e for e, _ in payloads} == {APPLE, GOOGLE}
    assert payloads[0][1] == {
        "title": "Time off approved",
        "body": "Your time-off request for Mon 18 Jan was approved.",
        "url": "/time-off/",
        "tag": f"stacks-{note.pk}",
    }
    assert {msg.ttl for _, msg in fake.sent} == {3 * 24 * 60 * 60}


def test_nothing_is_pushed_for_a_kind_turned_off(fake, org, student_user, django_capture_on_commit_callbacks):
    device(student_user)
    PushPreference.objects.create(
        organization=org, user=student_user, category=NotificationCategory.REMINDER, push=False
    )
    with django_capture_on_commit_callbacks(execute=True):
        notify(student_user, org, NotificationCategory.REMINDER, "Reminder", "Please enter availability")
    assert fake.sent == []


def test_nothing_is_pushed_to_an_inactive_account(
    fake, org, student_user, django_capture_on_commit_callbacks
):
    device(student_user)
    student_user.is_active = False
    student_user.save()
    with django_capture_on_commit_callbacks(execute=True):
        notify(student_user, org, NotificationCategory.PUBLISHED, "Published", "Your schedule changed")
    assert fake.sent == []


def test_nothing_is_pushed_until_the_transaction_commits(
    fake, org, student_user, django_capture_on_commit_callbacks
):
    device(student_user)
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        notify(student_user, org, NotificationCategory.TIME_OFF, "Time off approved", "Approved")  # no email
    assert fake.sent == [] and len(callbacks) == 1


def test_a_person_without_devices_queues_nothing(fake, org, student_user, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        notify(student_user, org, NotificationCategory.TIME_OFF, "Time off approved", "Approved")  # no email
    assert callbacks == []  # only the in-app notification


def test_another_persons_devices_never_get_it(
    fake, org, student_user, supervisor, django_capture_on_commit_callbacks
):
    device(supervisor, GOOGLE)
    device(student_user)
    with django_capture_on_commit_callbacks(execute=True):
        notify(student_user, org, NotificationCategory.PUBLISHED, "Published", "Your schedule changed")
    assert [e for e, _ in sent_payloads(fake)] == [APPLE]


def test_a_time_off_decision_reaches_the_phone(
    fake, supervisor, student_user, employee, django_capture_on_commit_callbacks
):
    device(student_user)
    t = TimeOff.objects.create(
        organization=employee.organization, employee=employee, start_date=MONDAY, end_date=MONDAY
    )
    with django_capture_on_commit_callbacks(execute=True):
        time_off.decide(t, supervisor, approve=False)
    ((_, payload),) = sent_payloads(fake)
    assert payload["title"] == "Time off not approved" and "not approved" in payload["body"]


def test_a_push_summary_is_short_and_skips_the_greeting_and_links():
    body = "Hi Avery,\n\n" + "word " * 100 + "\n\nhttps://stacks.example/x\n"
    text = push.summary(body)
    assert not text.startswith("Hi") and len(text) <= 180 and text.endswith("…")
    assert push.summary("Hi Avery,\n\nhttps://stacks.example/x\n") == ""


def test_a_stored_summary_wins_over_the_body(fake, org, student_user, django_capture_on_commit_callbacks):
    device(student_user)
    with django_capture_on_commit_callbacks(execute=True):
        notify(
            student_user,
            org,
            NotificationCategory.PUBLISHED,
            "Published",
            "Hi Avery,\n\nLong body",
            summary="2 shifts added",
        )
    assert sent_payloads(fake)[0][1]["body"] == "2 shifts added"


def test_a_push_with_no_link_opens_the_notifications_page(
    fake, org, student_user, django_capture_on_commit_callbacks
):
    device(student_user)
    with django_capture_on_commit_callbacks(execute=True):
        notify(student_user, org, NotificationCategory.REMINDER, "Reminder", "Please enter availability")
    assert sent_payloads(fake)[0][1]["url"] == reverse("notifications")


# --- Push service answers ---


def deliver_one(fake, user, org, outcome, django_capture_on_commit_callbacks, endpoint=APPLE):
    fake.outcomes[endpoint] = outcome
    with django_capture_on_commit_callbacks(execute=True):
        notify(user, org, NotificationCategory.PUBLISHED, "Published", "Your schedule changed")


def test_a_device_the_push_service_says_is_gone_is_dropped(
    fake, org, student_user, django_capture_on_commit_callbacks
):
    device(student_user)
    deliver_one(fake, student_user, org, Result(Outcome.GONE, 410), django_capture_on_commit_callbacks)
    assert not PushSubscription.objects.exists()


def test_a_device_refused_three_times_in_a_row_is_dropped(
    fake, org, student_user, django_capture_on_commit_callbacks
):
    sub = device(student_user)
    for expected in (1, 2):
        deliver_one(
            fake, student_user, org, Result(Outcome.REJECTED, 403), django_capture_on_commit_callbacks
        )
        sub.refresh_from_db()
        assert sub.failures == expected
    deliver_one(fake, student_user, org, Result(Outcome.REJECTED, 403), django_capture_on_commit_callbacks)
    assert not PushSubscription.objects.exists()


def test_a_success_clears_earlier_failures(fake, org, student_user, django_capture_on_commit_callbacks):
    sub = device(student_user, failures=2)
    deliver_one(fake, student_user, org, Result(Outcome.SENT, 201), django_capture_on_commit_callbacks)
    sub.refresh_from_db()
    assert sub.failures == 0 and sub.last_sent_at is not None


def test_a_busy_push_service_is_tried_three_times_and_the_device_kept(
    fake, org, student_user, django_capture_on_commit_callbacks
):
    sub = device(student_user)
    for _ in range(3):  # three notifications during an outage
        deliver_one(fake, student_user, org, Result(Outcome.RETRY, 503), django_capture_on_commit_callbacks)
    assert len(fake.sent) == 9  # STACKS_PUSH_INLINE retries at once in tests
    sub.refresh_from_db()
    assert sub.failures == 0  # a busy service says nothing about the device


@pytest.mark.parametrize(
    ("attempt", "retry_after", "delay"),
    [(1, None, 60), (2, None, 240), (1, 1800, 1800), (1, 86400, 3600), (2, 30, 240)],
)
def test_retries_wait_for_the_backoff_or_the_push_services_retry_after(
    fake, org, student_user, monkeypatch, attempt, retry_after, delay
):
    device(student_user)
    note = Notification.objects.create(
        organization=org, user=student_user, category="published", subject="S", body="B"
    )
    fake.outcomes[APPLE] = Result(Outcome.RETRY, 429, retry_after)
    calls = []
    monkeypatch.setattr(push, "dispatch", lambda *a, **k: calls.append((a, k)))
    push.deliver(note.pk, attempt=attempt)
    ((args, kwargs),) = calls
    assert args[2] == attempt + 1 and kwargs["delay"] == delay


def test_a_test_to_a_busy_push_service_does_not_count_against_the_device(fake, as_user, student_user):
    sub = device(student_user)
    fake.outcomes[APPLE] = Result(Outcome.RETRY, 503)
    as_user(student_user).post(reverse("push_test"))
    sub.refresh_from_db()
    assert sub.failures == 0


def test_a_bad_ttl_setting_turns_push_off_and_charges_no_device(settings, fake, org, student_user):
    settings.STACKS_PUSH_TTL = -1
    assert not push.enabled()


def test_a_server_side_mistake_charges_no_device(fake, org, student_user, monkeypatch):
    sub = device(student_user)
    note = Notification.objects.create(
        organization=org, user=student_user, category="published", subject="S", body="B"
    )
    monkeypatch.setattr(push, "message_for", lambda n: webpush.Message(b"{}", ttl=60, urgency="urgent"))
    with pytest.raises(ValueError):
        push.deliver(note.pk)  # the worker records a failed job; the device is not to blame
    sub.refresh_from_db()
    assert sub.failures == 0


def test_a_failed_email_stops_no_one_elses_email_or_push(
    fake,
    org,
    location,
    supervisor,
    employee,
    student_user,
    pay_role,
    monkeypatch,
    caplog,
    django_capture_on_commit_callbacks,
):
    import smtplib

    from app.people.models import Employee
    from app.scheduling import notify as notify_module
    from app.scheduling import services
    from app.scheduling.models import SchedulePeriod, Shift
    from tests.conftest import make_user

    other_user = make_user(org, "blake@example.edu", "employee")
    other = Employee.objects.create(
        organization=org, full_name="Blake Sample", preferred_name="Blake", user=other_user
    )
    device(student_user)
    device(other_user, GOOGLE)
    period = SchedulePeriod.objects.create(organization=org, location=location, start_date=MONDAY, weeks=2)
    for i, person in enumerate((employee, other)):
        Shift.objects.create(
            organization=org,
            location=location,
            period=period,
            employee=person,
            date=MONDAY,
            start_min=540 + 60 * i,
            end_min=720 + 60 * i,
            pay_role=pay_role,
        )
    sent_mail = []

    def failing_send_mail(subject, body, sender, to):
        sent_mail.append(to)
        raise smtplib.SMTPRecipientsRefused({to[0]: (550, b"no")})

    monkeypatch.setattr(notify_module, "send_mail", failing_send_mail)
    with django_capture_on_commit_callbacks(execute=True):
        services.publish(period, supervisor)
    assert len(sent_mail) == 2  # the second email was still tried
    assert {e for e, _ in sent_payloads(fake)} == {APPLE, GOOGLE}
    assert "avery@example.edu" not in caplog.text and "blake@example.edu" not in caplog.text


def test_pushes_go_ahead_of_queued_solves():
    from app.scheduling.tasks import run_solve, send_push, send_test_push

    assert send_push.priority > run_solve.priority and send_test_push.priority > run_solve.priority


def test_a_device_with_bad_stored_keys_is_charged_and_the_others_still_get_it(
    fake, org, student_user, django_capture_on_commit_callbacks
):
    device(student_user, p256dh="B" + "A" * 86)  # stored before keys were checked: not a curve point
    device(student_user, GOOGLE)
    with django_capture_on_commit_callbacks(execute=True):
        notify(student_user, org, NotificationCategory.PUBLISHED, "Published", "Your schedule changed")
    assert [e for e, _ in sent_payloads(fake)] == [GOOGLE]
    assert PushSubscription.objects.get(endpoint=APPLE).failures == 1


# --- Test notification ---


def test_a_test_notification_goes_to_the_persons_own_devices(fake, as_user, student_user, supervisor):
    device(student_user)
    device(supervisor, GOOGLE)
    resp = as_user(student_user).post(reverse("push_test"), follow=True)
    assert "Test notification sent to 1 device" in resp.content.decode()
    ((endpoint, payload),) = sent_payloads(fake)
    assert endpoint == APPLE and payload["title"] == "Test notification"
    assert not Notification.objects.exists()


# --- Drafts never notify; publishing does (CLAUDE.md §2.2, scenario 16) ---


def test_a_draft_shift_pushes_nothing_and_publishing_pushes_once(
    fake, org, location, supervisor, student_user, employee, pay_role, django_capture_on_commit_callbacks
):
    from app.scheduling import services
    from app.scheduling.models import SchedulePeriod, Shift

    device(student_user)
    period = SchedulePeriod.objects.create(organization=org, location=location, start_date=MONDAY, weeks=2)
    with django_capture_on_commit_callbacks(execute=True):
        Shift.objects.create(
            organization=org,
            location=location,
            period=period,
            employee=employee,
            date=MONDAY,
            start_min=540,
            end_min=720,
            pay_role=pay_role,
        )
    assert fake.sent == []
    with django_capture_on_commit_callbacks(execute=True):
        services.publish(period, supervisor)
    ((_, payload),) = sent_payloads(fake)
    assert payload["title"].startswith("Your schedule for") and payload["body"] == "1 shift added."
    assert payload["url"] == reverse("my_schedule")


# --- Plain text, not HTML ---


def test_a_reminder_shows_the_term_name_as_typed(
    fake, org, student_user, employee, django_capture_on_commit_callbacks
):
    from django.core import mail

    from app.availability import reminders
    from app.core.models import Term

    device(student_user)
    term = Term.objects.create(
        organization=org, name="Fall & Winter '26", start_date=date(2026, 9, 1), end_date=date(2026, 12, 18)
    )
    with django_capture_on_commit_callbacks(execute=True):
        reminders.send(term, date(2026, 11, 4), by_hand=True)
    ((_, payload),) = sent_payloads(fake)
    assert "Fall & Winter '26" in payload["body"] and "&amp;" not in payload["body"]
    assert "Fall & Winter '26" in Notification.objects.get().body
    assert "Fall & Winter '26" in mail.outbox[0].body


# --- Device names ---


def test_device_labels_name_the_browser_and_system():
    assert push.device_label(IPHONE_UA) == "Safari on iPhone"
    android = "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Mobile Safari/537.36"
    assert push.device_label(android) == "Chrome on Android"
    edge = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36 Edg/141.0"
    assert push.device_label(edge) == "Edge on Windows"
    assert push.device_label("") == "A browser"


# --- Records requests (CLAUDE.md §6) ---


def test_a_records_export_lists_devices_without_their_keys(fake, org, student_user, employee):
    from app.people import records

    device(student_user, device="Safari on iPhone")
    PushPreference.objects.create(organization=org, user=student_user, category="reminder", push=False)
    data = records.export_employee(employee)
    text = json.dumps(data)
    assert data["account"]["push_devices"][0]["device"] == "Safari on iPhone"
    assert data["account"]["push_settings"] == {"reminder": False}
    assert APPLE not in text and P256DH not in text and AUTH not in text


def test_erasing_removes_push_settings_and_devices(fake, org, student_user, employee):
    from app.people import records

    device(student_user)
    PushPreference.objects.create(organization=org, user=student_user, category="reminder", push=False)
    employee.archived_at = timezone.now()
    employee.save()
    records.erase_employee(employee)
    assert not PushSubscription.objects.exists() and not PushPreference.objects.exists()


def test_erasing_keeps_devices_used_for_another_organization(fake, org, other_org, student_user, employee):
    from app.core.models import Membership, Role
    from app.people import records

    Membership.objects.create(user=student_user, organization=other_org, role=Role.EMPLOYEE)
    device(student_user)
    employee.archived_at = timezone.now()
    employee.save()
    records.erase_employee(employee)
    assert PushSubscription.objects.exists()


# --- The service worker and every page ---


def test_the_service_worker_gets_the_push_addresses(fake, client, settings):
    body = client.get("/sw.js").content.decode()
    config = json.loads(body.split("const CONFIG = ", 1)[1].split(";\n", 1)[0])
    assert config["push"]["dropUrl"] == reverse("push_drop")
    assert config["push"]["renewUrl"] == reverse("push_renew")
    assert config["push"]["publicKey"] == webpush.public_key_text(
        webpush.private_key_from_text(settings.STACKS_VAPID_PRIVATE_KEY)
    )
    assert config["badge"].endswith("icon-badge-96.png")


def test_the_service_worker_has_no_push_addresses_when_push_is_off(settings, client):
    settings.STACKS_VAPID_PRIVATE_KEY = ""
    body = client.get("/sw.js").content.decode()
    assert '"push": null' in body


def test_signed_in_pages_carry_the_device_check(fake, as_user, student_user):
    page = as_user(student_user).get(reverse("home")).content.decode()
    assert f'data-push-check="{reverse("push_check")}"' in page
    assert f'data-push-user="{student_user.pk}"' in page


def test_the_sign_in_page_has_no_device_check(fake, client):
    assert "data-push-check" not in client.get(reverse("account_login")).content.decode()


@pytest.mark.parametrize(
    ("key", "subject", "ttl", "hosts", "ids"),
    [
        ("good", "mailto:it@library.example.edu", 60, [], []),
        ("", "", 60, [], []),  # push not set up: nothing to say
        ("not-a-key", "mailto:it@library.example.edu", 60, [], ["stacks.W001"]),
        ("encrypted", "mailto:it@library.example.edu", 60, [], ["stacks.W001"]),
        ("good", "mailto:stacks@localhost", 60, [], ["stacks.W002"]),
        ("good", "mailto:it@library.example.edu", -1, [], ["stacks.W004"]),
        ("good", "mailto:it@library.example.edu", 60, ["*.push.example.org", "push.example.org"], []),
        ("good", "mailto:it@library.example.edu", 60, ["https://push.example.org/"], ["stacks.W003"]),
    ],
)
def test_the_start_up_check_names_each_push_setting_problem(settings, key, subject, ttl, hosts, ids):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    from app.scheduling import checks

    if key == "good":
        key = webpush.generate_vapid_key()
    elif key == "encrypted":
        key = (
            ec.generate_private_key(ec.SECP256R1())
            .private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.BestAvailableEncryption(b"passphrase"),
            )
            .decode()
        )
    settings.STACKS_VAPID_PRIVATE_KEY = key
    settings.STACKS_VAPID_SUBJECT = subject
    settings.STACKS_PUSH_TTL = ttl
    settings.STACKS_PUSH_EXTRA_HOSTS = hosts
    assert [w.id for w in checks.push_settings(None)] == ids
    if "stacks.W001" in ids:
        assert not push.enabled()  # an unusable key turns push off, it doesn't break pages


def test_an_encrypted_key_leaves_signed_in_pages_working(settings, as_user, student_user):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    settings.STACKS_VAPID_SUBJECT = "mailto:it@library.example.edu"
    settings.STACKS_VAPID_PRIVATE_KEY = (
        ec.generate_private_key(ec.SECP256R1())
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"passphrase"),
        )
        .decode()
    )
    assert as_user(student_user).get(reverse("home")).status_code == 200


def test_the_vapid_keys_command_prints_a_usable_key():
    import io

    from django.core.management import call_command

    out = io.StringIO()
    call_command("vapid_keys", stdout=out)
    line = out.getvalue().splitlines()[0]
    assert line.startswith("STACKS_VAPID_PRIVATE_KEY=")
    key = line.removeprefix("STACKS_VAPID_PRIVATE_KEY=")
    assert webpush.public_key_text(webpush.private_key_from_text(key)) in out.getvalue()


def test_another_supervisor_can_turn_push_on_too(fake, org):
    boss = make_user(org, "boss@example.edu", "supervisor")
    client = Client()
    client.force_login(boss)
    assert post_json(client, "push_subscribe", subscription_json(GOOGLE)).status_code == 201


def test_the_device_check_needs_sign_in_and_the_csrf_token(fake, client, student_user, supervisor):
    device(supervisor)
    assert post_json(client, "push_check", {"endpoint": APPLE, "auth": AUTH}).status_code == 302
    strict = Client(enforce_csrf_checks=True)
    strict.force_login(student_user)
    assert post_json(strict, "push_check", {"endpoint": APPLE, "auth": AUTH}).status_code == 403
    assert PushSubscription.objects.exists()  # neither could drop the supervisor's device


@pytest.mark.parametrize(
    ("added", "removed", "text"),
    [
        (1, 0, "1 shift added."),
        (2, 1, "2 shifts added, 1 removed."),
        (0, 1, "1 shift removed."),
        (0, 3, "3 shifts removed."),
        (0, 0, "Your schedule was published."),
    ],
)
def test_the_lock_screen_line_for_a_published_schedule_counts_shifts(added, removed, text):
    from app.scheduling.services import PersonChanges, _counted

    assert _counted(PersonChanges(added=["x"] * added, removed=["y"] * removed)) == text
