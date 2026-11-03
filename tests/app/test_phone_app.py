# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stacks as an installable phone app (D124) and "Stay signed in" (D123). The service worker's
offline behaviour runs in a real browser in tests/browser/test_phone_app.py."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from unittest import mock

import pytest
from allauth.account.signals import user_logged_in
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.http import HttpResponse
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from app.accounts import sessions
from app.accounts.models import User
from tests.conftest import PASSWORD

pytestmark = pytest.mark.django_db
STATIC = Path(settings.BASE_DIR) / "app" / "static"
DAY = 24 * 60 * 60


def static_file(url: str) -> Path:
    return STATIC / url.removeprefix("/" + settings.STATIC_URL.strip("/") + "/")


# --- Manifest, service worker and pages ---


def test_the_manifest_describes_an_installable_app(client):
    resp = client.get(reverse("manifest"))
    assert resp.status_code == 200 and resp["Content-Type"] == "application/manifest+json"
    manifest = json.loads(resp.content)
    assert manifest["name"] == settings.STACKS_SITE_NAME
    assert manifest["start_url"] == "/" and manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    purposes = {i.get("purpose", "any") for i in manifest["icons"]}
    assert purposes == {"any", "maskable"}
    assert {i["sizes"] for i in manifest["icons"]} >= {"192x192", "512x512"}
    for icon in manifest["icons"]:
        assert static_file(icon["src"]).is_file(), icon["src"]


@override_settings(STACKS_SITE_NAME="Hillside Library Scheduling")
def test_the_manifest_uses_the_librarys_own_name(client):
    manifest = json.loads(client.get(reverse("manifest")).content)
    assert manifest["name"] == "Hillside Library Scheduling"
    assert len(manifest["short_name"]) <= 12  # fits under a home-screen icon


def test_the_service_worker_is_served_from_the_root_and_never_cached(client):
    resp = client.get("/sw.js")
    assert resp.status_code == 200 and resp["Content-Type"].startswith("text/javascript")
    assert resp["Cache-Control"] == "no-cache"
    body = resp.content.decode()
    assert "{{" not in body and "{%" not in body
    config = json.loads(body.split("const CONFIG = ", 1)[1].split(";\n", 1)[0])
    assert config["offlineUrl"] == reverse("offline")
    assert config["savedPages"] == [reverse("home"), reverse("my_schedule"), reverse("published_schedule")]
    assert config["loginPath"] == reverse("account_login") and config["logoutPath"] == reverse(
        "account_logout"
    )
    for url in config["precache"][1:]:
        assert static_file(url).is_file(), url


def test_the_offline_page_needs_no_sign_in_and_shows_no_ones_navigation(client, as_user, student_user):
    assert "You're offline" in client.get(reverse("offline")).content.decode()
    page = as_user(student_user).get(reverse("offline")).content.decode()
    assert 'class="tab-bar"' not in page and "My shifts" not in page


def test_the_install_help_is_public(client):
    page = client.get(reverse("install_help")).content.decode()
    assert "Add to Home Screen" in page and "Install app" in page
    assert reverse("account_login") in page


def test_the_install_help_links_an_employee_to_their_calendar_link(as_user, student_user, supervisor):
    assert (
        reverse("calendar_feed_manage") in as_user(student_user).get(reverse("install_help")).content.decode()
    )
    assert (
        reverse("calendar_feed_manage")
        not in as_user(supervisor).get(reverse("install_help")).content.decode()
    )


def test_every_page_links_the_manifest_icons_and_hidden_offline_note(as_user, student_user):
    page = as_user(student_user).get(reverse("home")).content.decode()
    assert f'<link rel="manifest" href="{reverse("manifest")}">' in page
    assert "apple-touch-icon.png" in page and "viewport-fit=cover" in page
    assert "data-offline-note hidden" in page  # the service worker reveals it on a saved copy
    assert "js/install.js" in page


def test_home_carries_the_install_card_hidden_until_the_script_shows_it(as_user, student_user):
    page = as_user(student_user).get(reverse("home")).content.decode()
    assert "data-install-card hidden" in page
    assert reverse("install_help") in page


def test_the_phone_more_sheet_links_the_install_help(as_user, student_user):
    page = as_user(student_user).get(reverse("home")).content.decode()
    sheet = page.split('class="tab-bar__sheet"', 1)[1]
    assert reverse("install_help") in sheet


def test_a_new_calendar_link_offers_one_tap_subscribing(as_user, student_user):
    page = as_user(student_user).post(reverse("calendar_feed_manage"), {"action": "create"}).content.decode()
    assert 'href="webcal://testserver/calendar/' in page
    assert "https://calendar.google.com/calendar/r?cid=webcal%3A%2F%2Ftestserver%2Fcalendar%2F" in page


# --- Stay signed in (D123) ---


def sign_in(client, remember: bool):
    data = {"login": "avery@example.edu", "password": PASSWORD}
    if remember:
        data["remember"] = "on"
    resp = client.post(reverse("account_login"), data)
    assert resp.status_code == 302
    return client.session


def test_the_sign_in_page_offers_stay_signed_in(client):
    page = client.get(reverse("account_login")).content.decode()
    assert "Stay signed in" in page and "90 days" in page
    assert "Remember Me" not in page


def test_a_ticked_sign_in_lasts_the_remember_period(client, student_user):
    session = sign_in(client, remember=True)
    assert not session.get_expire_at_browser_close()
    assert session.get_expiry_age() == 90 * DAY
    assert session[sessions.REMEMBERED] is True


def test_an_unticked_sign_in_ends_with_the_browser(client, student_user):
    session = sign_in(client, remember=False)
    assert session.get_expire_at_browser_close()
    assert sessions.REMEMBERED not in session


@override_settings(STACKS_REMEMBER_DAYS=0, ACCOUNT_SESSION_REMEMBER=False)
def test_a_zero_remember_period_removes_the_option(client, student_user):
    assert "Stay signed in" not in client.get(reverse("account_login")).content.decode()


def _expire_date(client):
    return Session.objects.get(session_key=client.session.session_key).expire_date


def test_a_remembered_sign_in_is_renewed_by_the_first_visit_each_day(client, student_user):
    sign_in(client, remember=True)
    session = client.session
    session[sessions.RENEWED_ON] = "2000-01-01"
    session.save()
    Session.objects.filter(session_key=session.session_key).update(
        expire_date=timezone.now() + timedelta(days=1)
    )
    client.get(reverse("home"))
    assert _expire_date(client) > timezone.now() + timedelta(days=89)
    assert client.session[sessions.RENEWED_ON] == timezone.localdate().isoformat()


def test_later_visits_the_same_day_leave_the_session_alone(client, student_user):
    sign_in(client, remember=True)
    soon = timezone.now() + timedelta(days=1)
    Session.objects.filter(session_key=client.session.session_key).update(expire_date=soon)
    client.get(reverse("home"))
    assert _expire_date(client) == soon


def test_an_unremembered_sign_in_is_not_extended(client, student_user):
    sign_in(client, remember=False)
    soon = timezone.now() + timedelta(hours=1)
    Session.objects.filter(session_key=client.session.session_key).update(expire_date=soon)
    client.get(reverse("home"))
    assert _expire_date(client) == soon


def _campus_request(rf, remember: bool):
    url = reverse("openid_connect_login", kwargs={"provider_id": "campus"})
    request = rf.post(url, {"remember": "on"} if remember else {})
    request.session = SessionStore()
    request.user = AnonymousUser()
    sessions.StaySignedInMiddleware(lambda r: HttpResponse())(request)
    return request


@pytest.mark.parametrize("remember", [True, False])
def test_campus_sign_in_keeps_the_box_choice_through_the_provider(rf, student_user, remember):
    request = _campus_request(rf, remember)
    assert request.session[sessions.SSO_CHOICE] is remember
    user_logged_in.send(sender=User, request=request, user=student_user, sociallogin=mock.Mock())
    assert sessions.SSO_CHOICE not in request.session
    if remember:
        assert request.session.get_expiry_age() == 90 * DAY and request.session[sessions.REMEMBERED]
    else:
        assert request.session.get_expiry_age() == settings.SESSION_COOKIE_AGE
        assert sessions.REMEMBERED not in request.session


def test_a_password_sign_in_ignores_a_leftover_campus_choice(client, student_user):
    session = client.session
    session[sessions.SSO_CHOICE] = True  # started campus sign-in, then came back
    session.save()
    session = sign_in(client, remember=False)
    assert session.get_expire_at_browser_close() and sessions.REMEMBERED not in session


@override_settings(
    SOCIALACCOUNT_PROVIDERS={
        "openid_connect": {
            "APPS": [
                {
                    "provider_id": "campus",
                    "name": "Campus",
                    "client_id": "c",
                    "secret": "s",
                    "settings": {"server_url": "https://idp.example.edu"},
                }
            ]
        }
    },
    OIDC_ALLOWED_DOMAINS=["example.edu"],
)
def test_the_campus_sign_in_button_has_its_own_stay_signed_in_box(client):
    page = client.get(reverse("account_login")).content.decode()
    sso = page.split('class="sso"', 1)[1]
    assert 'name="remember"' in sso and "Stay signed in" in sso
