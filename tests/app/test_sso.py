# SPDX-License-Identifier: AGPL-3.0-or-later
"""Campus sign-in over OpenID Connect (SPEC §11, D5, D118): who may sign in, and what is kept.

These drive allauth's login flow up to the point where it would sign the person in, with
claims made up here. The whole round trip against a real provider is in
tests/browser/test_sso_keycloak.py."""

from __future__ import annotations

import html

import pytest
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import get_adapter as get_social_adapter
from allauth.socialaccount.internal.flows.login import pre_social_login
from allauth.socialaccount.models import SocialAccount, SocialLogin
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.backends.db import SessionStore
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from app.accounts.models import User
from app.core.permissions import NO_ACCESS
from app.people.records import erase_employee, export_employee

pytestmark = pytest.mark.django_db

PROVIDERS = {
    "openid_connect": {
        "APPS": [
            {
                "provider_id": "campus",
                "name": "Campus",
                "client_id": "test-client",
                "secret": "test-only-secret",
                "settings": {"server_url": "https://idp.example.edu"},
            }
        ]
    }
}


@pytest.fixture
def sso():
    with override_settings(SOCIALACCOUNT_PROVIDERS=PROVIDERS, OIDC_ALLOWED_DOMAINS=["example.edu"]):
        yield


def claims(email="avery@example.edu", **extra):
    return {"sub": f"sub-{email}", "email": email, "email_verified": True, **extra}


def page_text(response) -> str:
    return html.unescape(response.content.decode())


def sign_in(rf, data, process="login", user=None):
    """Run allauth's pre-login step for a provider response. Returns the SocialLogin, or the
    refusal page when the adapter turns the person away."""
    request = rf.get("/")
    request.user = user or AnonymousUser()
    request.access = NO_ACCESS
    request.session = SessionStore()
    provider = get_social_adapter().get_provider(request, "campus")
    login = provider.sociallogin_from_response(request, {"id_token": data})
    login.state["process"] = process
    try:
        pre_social_login(request, login)
    except ImmediateHttpResponse as refused:
        return refused.response
    return login


def test_an_invited_person_is_linked_by_verified_campus_email(sso, rf, student_user):
    login = sign_in(rf, claims(email="Avery@Example.edu", name="Avery Example", picture="https://x"))
    assert login.user == student_user
    account = SocialAccount.objects.get(user=student_user)
    assert account.uid == "sub-Avery@Example.edu"
    # Only the claims Stacks needs are kept (SPEC §11).
    assert account.extra_data == {
        "id_token": {"sub": "sub-Avery@Example.edu", "email": "Avery@Example.edu", "email_verified": True}
    }


def test_a_linked_person_signs_in_again(sso, rf, student_user):
    sign_in(rf, claims())
    login = sign_in(rf, claims(name="Avery Example"))
    assert login.is_existing and login.user == student_user
    assert "name" not in SocialAccount.objects.get(user=student_user).extra_data["id_token"]


@pytest.mark.parametrize("verified", [False, "false", None])
def test_an_unverified_email_is_refused(sso, rf, student_user, verified):
    response = sign_in(rf, claims(email_verified=verified))
    assert response.status_code == 403
    assert "didn't confirm an email address" in page_text(response)
    assert not SocialAccount.objects.exists()


def test_a_string_true_counts_as_verified(sso, rf, student_user):
    assert sign_in(rf, claims(email_verified="true")).user == student_user


def test_an_address_outside_the_campus_domains_is_refused(sso, rf, org):
    User.objects.create_user("avery@example.org")
    response = sign_in(rf, claims(email="avery@example.org"))
    assert response.status_code == 403
    assert "Use your campus account" in page_text(response)
    assert not SocialAccount.objects.exists()


def test_a_lookalike_subdomain_is_refused(sso, rf, org):
    User.objects.create_user("avery@mail.example.edu")
    assert sign_in(rf, claims(email="avery@mail.example.edu")).status_code == 403


@override_settings(OIDC_REQUIRE_HD=True)
def test_google_accounts_must_belong_to_the_workspace(sso, rf, student_user):
    # A personal Google account can carry a campus address without being in the Workspace.
    assert sign_in(rf, claims()).status_code == 403
    assert sign_in(rf, claims(hd="example.org")).status_code == 403
    assert sign_in(rf, claims(hd="example.edu")).user == student_user


def test_nobody_gets_an_account_by_signing_in(sso, rf, org):
    response = sign_in(rf, claims(email="stranger@example.edu"))
    assert response.status_code == 403
    page = page_text(response)
    assert "No Stacks account uses stranger@example.edu" in page and "Ask your supervisor" in page
    assert not User.objects.exists()


def test_a_deactivated_account_is_not_linked(sso, rf, student_user):
    student_user.is_active = False
    student_user.save()
    assert sign_in(rf, claims()).status_code == 403
    assert not SocialAccount.objects.exists()


def test_connecting_needs_the_same_email_as_the_signed_in_account(sso, rf, student_user, supervisor):
    assert sign_in(rf, claims(), process="connect", user=supervisor).status_code == 403
    assert isinstance(sign_in(rf, claims(), process="connect", user=student_user), SocialLogin)


def test_the_sign_in_page_offers_campus_sign_in_only_when_configured(client, sso):
    page = client.get(reverse("account_login")).content.decode()
    assert "Sign in with Campus" in page and 'method="post"' in page
    with override_settings(SOCIALACCOUNT_PROVIDERS={}):
        assert "Sign in with" not in client.get(reverse("account_login")).content.decode()


def test_records_export_and_erasure_cover_sign_in_links(sso, rf, student_user, employee):
    sign_in(rf, claims())
    exported = export_employee(employee)["account"]["single_sign_on"]
    assert [a["subject"] for a in exported] == ["sub-avery@example.edu"]
    employee.archived_at = timezone.now()
    employee.save()
    erase_employee(employee)
    assert not SocialAccount.objects.exists()
