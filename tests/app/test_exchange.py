# SPDX-License-Identifier: AGPL-3.0-or-later
"""Claiming open shifts and covering co-workers (SPEC §9.2): employees ask, a supervisor
approves after the validation suite runs on the would-be result."""
# ruff: noqa: F811 (the `setup` fixture is shared from test_published)

from __future__ import annotations

from unittest import mock

import pytest
from django.core import mail
from django.urls import reverse

from app.core.models import Role
from app.people.models import Employee
from app.scheduling.models import ExchangeStatus, Notification, Shift, ShiftExchange
from tests.app.test_published import MONDAY, START, setup  # noqa: F401
from tests.conftest import make_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def casey_user(org, pay_role):
    user = make_user(org, "casey@example.edu", Role.EMPLOYEE)
    Employee.objects.create(
        organization=org,
        full_name="Casey Sample",
        preferred_name="Casey",
        user=user,
        default_pay_role=pay_role,
    )
    return user


def post(client, **data):
    with mock.patch("django.utils.timezone.localdate", return_value=START):
        return client.post(reverse("shift_exchange"), data, follow=True)


def open_shift():
    return Shift.objects.get(date=MONDAY, employee__isnull=True)


def test_exchange_page_lists_open_shifts_and_my_shifts(as_user, student_user, setup):
    with mock.patch("django.utils.timezone.localdate", return_value=START):
        page = as_user(student_user).get(reverse("shift_exchange")).content.decode()
    assert "Ask for this shift" in page and "Offer for cover" in page
    assert "1pm–3pm" in page


def test_claim_an_open_shift_then_supervisor_approves(as_user, student_user, supervisor, employee, setup):
    shift = open_shift()
    notes_before, mail_before = Notification.objects.count(), len(mail.outbox)
    post(as_user(student_user), action="claim", shift=shift.pk)
    ex = ShiftExchange.objects.get()
    assert ex.status == ExchangeStatus.PENDING and ex.taker == employee
    sup = as_user(supervisor)
    page = sup.get(reverse("review_queue")).content.decode()
    assert "Avery asks for this open shift" in page and "Passes" in page
    resp = sup.post(reverse("review_queue"), {"action": "approve_exchange", "exchange": ex.pk}, follow=True)
    shift.refresh_from_db()
    ex.refresh_from_db()
    assert shift.employee == employee and ex.status == ExchangeStatus.APPROVED
    assert "Validation:" in resp.content.decode()
    # Nothing is sent until a supervisor publishes.
    assert (Notification.objects.count(), len(mail.outbox)) == (notes_before, mail_before)


def test_offer_cover_volunteer_and_approve(as_user, student_user, casey_user, supervisor, setup):
    _, _, mine = setup
    post(as_user(student_user), action="offer", shift=mine.pk)
    ex = ShiftExchange.objects.get()
    assert ex.status == ExchangeStatus.OPEN
    with mock.patch("django.utils.timezone.localdate", return_value=START):
        page = as_user(casey_user).get(reverse("shift_exchange")).content.decode()
    assert "from Avery" in page and "I can take it" in page
    post(as_user(casey_user), action="volunteer", exchange=ex.pk)
    ex.refresh_from_db()
    casey = Employee.objects.get(preferred_name="Casey")
    assert ex.status == ExchangeStatus.PENDING and ex.taker == casey
    as_user(supervisor).post(reverse("review_queue"), {"action": "approve_exchange", "exchange": ex.pk})
    mine.refresh_from_db()
    assert mine.employee == casey


def test_volunteer_already_working_then_is_refused(as_user, student_user, setup, org):
    _, blake, mine = setup
    blake.user = make_user(org, "blake-user@example.edu", Role.EMPLOYEE)
    blake.save()
    post(as_user(student_user), action="offer", shift=mine.pk)
    ex = ShiftExchange.objects.get()
    resp = post(as_user(blake.user), action="volunteer", exchange=ex.pk)
    assert "already working at that time" in resp.content.decode()
    ex.refresh_from_db()
    assert ex.status == ExchangeStatus.OPEN


def test_volunteer_withdraws_and_offer_goes_back_up(as_user, student_user, casey_user, setup):
    _, _, mine = setup
    post(as_user(student_user), action="offer", shift=mine.pk)
    ex = ShiftExchange.objects.get()
    post(as_user(casey_user), action="volunteer", exchange=ex.pk)
    post(as_user(casey_user), action="withdraw", exchange=ex.pk)
    ex.refresh_from_db()
    assert ex.status == ExchangeStatus.OPEN and ex.taker is None
    post(as_user(student_user), action="withdraw", exchange=ex.pk)
    ex.refresh_from_db()
    assert ex.status == ExchangeStatus.WITHDRAWN


def test_only_published_shifts_can_be_claimed_or_offered(as_user, student_user, setup, org, location):
    period, _, mine = setup
    draft_open = Shift.objects.create(
        organization=org, location=location, period=period, date=MONDAY, start_min=1080, end_min=1200
    )
    resp = post(as_user(student_user), action="claim", shift=draft_open.pk)
    assert "isn&#x27;t an open shift on the published schedule" in resp.content.decode()
    mine.start_min = 600  # a draft retime: the published shift no longer matches
    mine.save()
    resp = post(as_user(student_user), action="offer", shift=mine.pk)
    assert "only offer your own shifts from the published schedule" in resp.content.decode()
    assert not ShiftExchange.objects.exists()


def test_approval_refused_when_the_shift_changed(as_user, student_user, supervisor, setup):
    shift = open_shift()
    post(as_user(student_user), action="claim", shift=shift.pk)
    ex = ShiftExchange.objects.get()
    shift.start_min = 720
    shift.end_min = 900
    shift.employee = Employee.objects.get(preferred_name="Blake")
    shift.save()
    sup = as_user(supervisor)
    assert "changed since the request" in sup.get(reverse("review_queue")).content.decode()
    resp = sup.post(reverse("review_queue"), {"action": "approve_exchange", "exchange": ex.pk}, follow=True)
    assert "changed since the request" in resp.content.decode()
    ex.refresh_from_db()
    assert ex.status == ExchangeStatus.PENDING


def test_students_cannot_approve(as_user, student_user, setup):
    shift = open_shift()
    post(as_user(student_user), action="claim", shift=shift.pk)
    ex = ShiftExchange.objects.get()
    resp = as_user(student_user).post(
        reverse("review_queue"), {"action": "approve_exchange", "exchange": ex.pk}
    )
    assert resp.status_code == 403


def test_past_shifts_cannot_be_claimed(as_user, student_user, setup):
    shift = open_shift()
    with mock.patch("django.utils.timezone.localdate", return_value=MONDAY.replace(day=20)):
        resp = as_user(student_user).post(
            reverse("shift_exchange"), {"action": "claim", "shift": shift.pk}, follow=True
        )
    assert "already happened" in resp.content.decode()
