# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phone notifications by web push (D125, phase 2 of the phone plan).

`notify()` calls `queue()` for every notification. A push goes out only when the site has a
VAPID key, the person's account is active, they haven't turned that kind off for phones, and
they have turned notifications on for at least one device. Sending happens in the worker
after the transaction commits, so nothing is pushed for work that rolls back, and a push
carries only what the in-app notification says: its subject, one line and a link.

The push service's answers decide a device's fate: gone (404/410) drops it at once, three
refusals in a row drop it, and a busy or unreachable service is tried three times in all
without counting against the device (it says nothing about the device). A device renewed by
the browser gets nothing until its owner next opens Stacks on it, signed in.
"""

from __future__ import annotations

import functools
import json
import logging
import re

from django.conf import settings
from django.db import transaction
from django.db.models import Case, F, Value, When
from django.urls import reverse
from django.utils import timezone

from adapters import webpush
from adapters.webpush import (
    Message,
    Outcome,
    PushSender,
    Result,
    Subscription,
    SubscriptionError,
    WebPushSender,
)
from app.scheduling.models import Notification, NotificationCategory, PushPreference, PushSubscription

log = logging.getLogger(__name__)

GIVE_UP_AFTER = 3  # refusals in a row before a device is dropped
MAX_ATTEMPTS = 3  # tries for a busy push service, the first included
MAX_DEVICES = 10  # per person; the least recently used goes first
MAX_RETRY_DELAY = 60 * 60  # never wait longer than this for a push service's Retry-After
SUMMARY_LENGTH = 180
TITLE_LENGTH = 120


@functools.lru_cache(maxsize=4)
def _key(text: str) -> str | None:
    """The public key for a VAPID private key, or None when the key won't load."""
    try:
        return webpush.public_key_text(webpush.private_key_from_text(text))
    except ValueError:
        return None


def enabled() -> bool:
    """Push is on when the site has a VAPID key that loads, a contact Apple accepts and a
    usable time to live. `checks.py` reports whichever is missing."""
    key = settings.STACKS_VAPID_PRIVATE_KEY
    return (
        bool(key)
        and _key(key) is not None
        and webpush.subject_ok(settings.STACKS_VAPID_SUBJECT)
        and settings.STACKS_PUSH_TTL >= 0
    )


def public_key() -> str:
    return _key(settings.STACKS_VAPID_PRIVATE_KEY) or ""


def hosts() -> tuple[str, ...]:
    return (*webpush.DEFAULT_HOSTS, *settings.STACKS_PUSH_EXTRA_HOSTS)


@functools.lru_cache(maxsize=1)
def _sender(key: str, subject: str, extra_hosts: tuple[str, ...]) -> WebPushSender:
    return WebPushSender(key, subject, hosts=(*webpush.DEFAULT_HOSTS, *extra_hosts))


def get_sender() -> PushSender:
    """One sender per worker process, so its VAPID tokens are reused (tests replace this)."""
    return _sender(
        settings.STACKS_VAPID_PRIVATE_KEY,
        settings.STACKS_VAPID_SUBJECT,
        tuple(settings.STACKS_PUSH_EXTRA_HOSTS),
    )


# --- Preferences ---


def wants_push(user, organization, category: str) -> bool:
    pref = PushPreference.objects.filter(user=user, organization=organization, category=category).first()
    return pref.push if pref is not None else True


def push_settings(user, organization) -> dict[str, bool]:
    return {c.value: wants_push(user, organization, c) for c in NotificationCategory}


# --- What a push says ---


def summary(body: str) -> str:
    """The first paragraph of a notification's body that isn't the greeting or a link,
    on one line and at most `SUMMARY_LENGTH` characters."""
    for para in re.split(r"\n\s*\n", body):
        text = " ".join(para.split())
        if not text or (text.startswith("Hi ") and text.endswith(",")):
            continue
        if re.fullmatch(r"(?:\S+: )?(?:https?://|/)\S*", text):
            continue
        return _shorten(text, SUMMARY_LENGTH)
    return ""


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def title(subject: str) -> str:
    """The subject without the site's name: the phone already shows which app it's from."""
    prefix = f"{settings.STACKS_SITE_NAME}:"
    text = subject.removeprefix(prefix).strip() if subject.startswith(prefix) else subject.strip()
    return _shorten(text[:1].upper() + text[1:], TITLE_LENGTH)


def _message(data: dict[str, str]) -> Message:
    return Message(json.dumps(data, ensure_ascii=False).encode("utf-8"), ttl=settings.STACKS_PUSH_TTL)


def message_for(note: Notification) -> Message:
    return _message(
        {
            "title": title(note.subject),
            "body": note.summary or summary(note.body),
            "url": note.link if note.link.startswith("/") else reverse("notifications"),
            "tag": f"stacks-{note.pk}",
        }
    )


# --- Queueing and delivery ---


def devices_of(user_or_id):
    """A person's devices that may get pushes: confirmed by the person (D125)."""
    user_id = getattr(user_or_id, "pk", user_or_id)
    return PushSubscription.objects.filter(user_id=user_id, confirmed_at__isnull=False)


def queue(note: Notification) -> None:
    """Called by `notify()`: push this notification after the transaction commits, if wanted."""
    user = note.user
    if not (enabled() and user.is_active and wants_push(user, note.organization, note.category)):
        return
    if not devices_of(user).exists():
        return
    pk = note.pk
    transaction.on_commit(lambda: _dispatch_after_commit(pk))


def _dispatch_after_commit(notification_id: int) -> None:
    """Runs after commit, next to other people's emails and pushes: a failure here must not
    stop theirs, or turn a committed change into an error page."""
    try:
        dispatch(notification_id)
    except Exception as exc:
        log.warning("Push for notification %s not queued: %s", notification_id, type(exc).__name__)


def dispatch(
    notification_id: int, subscription_id: int | None = None, attempt: int = 1, delay: int = 0
) -> None:
    if settings.STACKS_PUSH_INLINE:
        deliver(notification_id, subscription_id, attempt)
        return
    from app.scheduling.tasks import send_push

    task = send_push.configure(schedule_in={"seconds": delay}) if delay else send_push
    task.defer(notification_id=notification_id, subscription_id=subscription_id, attempt=attempt)


def deliver(notification_id: int, subscription_id: int | None = None, attempt: int = 1) -> None:
    """Send one notification to the person's devices (or one of them, on a retry)."""
    note = Notification.objects.select_related("user", "organization").filter(pk=notification_id).first()
    if note is None or not enabled() or not note.user.is_active:
        return
    if not wants_push(note.user, note.organization, note.category):
        return
    devices = devices_of(note.user)
    if subscription_id is not None:
        devices = devices.filter(pk=subscription_id)
    message = message_for(note)
    for sub in devices:
        result = _send(sub, message)
        if result.outcome is not Outcome.RETRY:
            continue
        if attempt < MAX_ATTEMPTS:
            backoff = 60 * 4 ** (attempt - 1)  # 1 minute, then 4
            delay = max(backoff, min(result.retry_after or 0, MAX_RETRY_DELAY))
            dispatch(notification_id, sub.pk, attempt + 1, delay=delay)
        else:
            # A busy or unreachable push service says nothing about the device: keep it.
            log.warning("Push to device %s not delivered after %s tries: %s", sub.pk, attempt, result.reason)


def queue_test(user) -> int:
    """Queue a test notification to the person's own devices. Returns how many devices."""
    n = devices_of(user).count()
    if not n:
        return 0
    if settings.STACKS_PUSH_INLINE:
        deliver_test(user.pk)
    else:
        from app.scheduling.tasks import send_test_push

        send_test_push.defer(user_id=user.pk)
    return n


def deliver_test(user_id: int) -> None:
    """Send the test notification, once, to each of the person's devices."""
    message = _message(
        {
            "title": "Test notification",
            "body": f"Notifications from {settings.STACKS_SITE_NAME} work on this device.",
            "url": reverse("email_settings"),
        }  # no tag: each test shows as a new notification, so a second test alerts again
    )
    for sub in devices_of(user_id).filter(user__is_active=True):
        result = _send(sub, message)
        if result.outcome is Outcome.RETRY:
            log.warning("Test push to device %s not delivered: %s", sub.pk, result.reason)


def _send(sub: PushSubscription, message: Message) -> Result:
    """Send to one device and record the answer. RETRY is left to the caller. A message the
    server built wrongly (bad settings) raises ValueError and charges no device."""
    subscription = Subscription(sub.endpoint, sub.p256dh, sub.auth)
    try:
        subscription.keys()
    except SubscriptionError:
        # A stored key that no longer checks out. Never log the endpoint or keys.
        log.warning("Push to device %s failed: bad subscription keys", sub.pk)
        _failed(sub)
        return Result(Outcome.REJECTED, reason="bad subscription keys")
    result = get_sender().send(subscription, message)
    if result.outcome is Outcome.SENT:
        PushSubscription.objects.filter(pk=sub.pk).update(failures=0, last_sent_at=timezone.now())
    elif result.outcome is Outcome.GONE:
        PushSubscription.objects.filter(pk=sub.pk).delete()
    elif result.outcome is Outcome.REJECTED:
        log.warning("Push to device %s refused: %s", sub.pk, result.reason)
        _failed(sub)
    return result


def _failed(sub: PushSubscription) -> None:
    PushSubscription.objects.filter(pk=sub.pk).update(failures=F("failures") + 1)
    PushSubscription.objects.filter(pk=sub.pk, failures__gte=GIVE_UP_AFTER).delete()


# --- Devices ---


def device_label(user_agent: str) -> str:
    """A short name for the device list, from the browser's user agent: "Safari on iPhone"."""
    ua = user_agent or ""
    system = next(
        (
            name
            for marker, name in (
                ("iPhone", "iPhone"),
                ("iPad", "iPad"),
                ("Android", "Android"),
                ("CrOS", "ChromeOS"),
                ("Macintosh", "Mac"),
                ("Windows", "Windows"),
                ("Linux", "Linux"),
            )
            if marker in ua
        ),
        "",
    )
    browser = next(
        (
            name
            for marker, name in (
                ("Edg", "Edge"),
                ("Firefox", "Firefox"),
                ("FxiOS", "Firefox"),
                ("SamsungBrowser", "Samsung Internet"),
                ("CriOS", "Chrome"),
                ("Chrome", "Chrome"),
                ("Safari", "Safari"),
            )
            if marker in ua
        ),
        "",
    )
    if browser and system:
        return f"{browser} on {system}"
    return browser or system or "A browser"


def save_device(user, sub: Subscription, user_agent: str, *, confirmed: bool = True) -> PushSubscription:
    """Store a device for `user`, taking it over from anyone who had it, and keep the person's
    newest `MAX_DEVICES`, confirmed ones first. `confirmed=False` (a renewal from the service
    worker, which has no session) holds pushes back until the owner's next signed-in visit."""
    now = timezone.now()
    device, _ = PushSubscription.objects.update_or_create(
        endpoint=sub.endpoint,
        defaults={
            "user": user,
            "p256dh": sub.p256dh,
            "auth": sub.auth,
            "device": device_label(user_agent)[:60],
            "last_seen_at": now,
            "confirmed_at": now if confirmed else None,
            "failures": 0,
        },
    )
    keep = PushSubscription.objects.filter(user=user).order_by(
        Case(
            When(confirmed_at__isnull=True, then=Value(1)), default=Value(0)
        ),  # awaiting owner: dropped first
        F("last_seen_at").desc(nulls_last=True),
        "-created_at",
    )
    stale = list(keep.values_list("pk", flat=True)[MAX_DEVICES:])
    PushSubscription.objects.filter(pk__in=stale).delete()
    return device
