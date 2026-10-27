# SPDX-License-Identifier: AGPL-3.0-or-later
"""Campus sign-in end to end against a local Keycloak (SPEC §11, D5, D118), never a real provider.

Needs Keycloak running with fixtures/keycloak/stacks-test-realm.json (see the README there)
and KEYCLOAK_URL pointing at it. Skipped otherwise; CI sets REQUIRE_KEYCLOAK so it can't
pass by being skipped."""

from __future__ import annotations

import os
import urllib.request

import pytest
from allauth.socialaccount.models import SocialAccount
from django.test import override_settings

from app.accounts.models import User

pytestmark = pytest.mark.django_db(transaction=True)
PASSWORD = "a-long-test-password"  # the realm's synthetic users share it


@pytest.fixture(scope="session")
def keycloak_realm():
    url = os.environ.get("KEYCLOAK_URL", "").rstrip("/")
    realm = f"{url}/realms/stacks-test"
    try:
        if not url:
            raise OSError("KEYCLOAK_URL is not set")
        urllib.request.urlopen(f"{realm}/.well-known/openid-configuration", timeout=5).close()  # noqa: S310
    except OSError as exc:
        if os.environ.get("REQUIRE_KEYCLOAK"):
            raise
        pytest.skip(f"Keycloak not available: {exc}")
    return realm


@pytest.fixture
def campus_sso(keycloak_realm):
    providers = {
        "openid_connect": {
            "OAUTH_PKCE_ENABLED": True,
            "APPS": [
                {
                    "provider_id": "campus",
                    "name": "Campus",
                    "client_id": "stacks",
                    "secret": "test-only-client-secret",
                    "settings": {"server_url": keycloak_realm},
                }
            ],
        }
    }
    with override_settings(
        SOCIALACCOUNT_PROVIDERS=providers, OIDC_ALLOWED_DOMAINS=["example.edu"], OIDC_REQUIRE_HD=True
    ):
        yield


def sign_in_with_campus(browser, live_server, username):
    context = browser.new_context()
    page = context.new_page()
    page.goto(live_server.url + "/accounts/login/")
    page.get_by_role("button", name="Sign in with Campus").click()
    page.fill("#username", username)
    page.fill("#password", PASSWORD)
    page.click("#kc-login")
    page.wait_for_load_state()
    return context, page


def test_an_invited_person_signs_in_with_campus_sso(browser, live_server, campus_sso, student_user):
    context, page = sign_in_with_campus(browser, live_server, "avery")
    assert page.url.rstrip("/") == live_server.url
    assert page.get_by_role("button", name="Sign out").count() > 0
    account = SocialAccount.objects.get(user=student_user)
    assert set(account.extra_data["id_token"]) <= {"iss", "sub", "email", "email_verified", "hd"}
    context.close()


def test_an_unverified_email_is_turned_away(browser, live_server, campus_sso, org):
    User.objects.create_user("casey@example.edu")
    context, page = sign_in_with_campus(browser, live_server, "casey")
    assert "Can't sign you in with Campus" in page.content()
    assert not SocialAccount.objects.exists()
    context.close()


def test_someone_without_a_stacks_account_is_turned_away(browser, live_server, campus_sso, org):
    context, page = sign_in_with_campus(browser, live_server, "stranger")
    assert "No Stacks account uses stranger@example.edu" in page.content()
    assert not User.objects.filter(email="stranger@example.edu").exists()
    context.close()


def test_campus_sign_in_still_asks_for_the_authenticator_code(browser, live_server, campus_sso, student_user):
    """Someone who turned on TOTP is asked for it after campus sign-in too (D118)."""
    from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret

    TOTP.activate(student_user, generate_totp_secret())
    context, page = sign_in_with_campus(browser, live_server, "avery")
    assert page.url.endswith("/accounts/2fa/authenticate/")
    assert page.locator("input[name=code]").count() == 1
    context.close()
