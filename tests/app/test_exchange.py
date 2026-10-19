# SPDX-License-Identifier: AGPL-3.0-or-later
"""Claiming open shifts and covering co-workers (SPEC §9.2): employees ask, a supervisor
approves after the validation suite runs on the would-be result."""
# ruff: noqa: F811 (the `setup` fixture is shared from test_published)

from __future__ import annotations

from datetime import timedelta
from unittest import mock

import pytest
from django.core import mail
from django.urls import reverse

from app.core.models import Role
from app.people.models import Employee, EmploymentType
from app.scheduling import services
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
    # The person who asked hears the decision in the app (D103); no email unless they turned it
    # on, and the shift itself still waits for publishing.
    assert (Notification.objects.count(), len(mail.outbox)) == (notes_before + 1, mail_before)
    note = Notification.objects.latest("pk")
    assert note.user == student_user and note.category == "exchange"
    assert "approved" in note.body and "once a supervisor publishes" in note.body


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


# --- Swaps and non-regular claims (D104) ----------------------------------------------------


@pytest.fixture
def casey_shift(org, location, supervisor, casey_user, setup, pay_role):
    period, _, _ = setup
    shift = Shift.objects.create(
        organization=org,
        location=location,
        period=period,
        employee=Employee.objects.get(user=casey_user),
        date=MONDAY + timedelta(days=1),
        start_min=540,
        end_min=720,
        pay_role=pay_role,
    )
    services.publish(period, supervisor)
    return shift


def ask_swap(client, mine, other):
    with mock.patch("django.utils.timezone.localdate", return_value=START):
        return client.post(reverse("shift_swap", args=[mine.pk]), {"other": other.pk}, follow=True)


def test_swap_asked_accepted_and_approved(
    as_user, student_user, casey_user, supervisor, employee, setup, casey_shift
):
    _, _, mine = setup
    casey = Employee.objects.get(user=casey_user)
    with mock.patch("django.utils.timezone.localdate", return_value=START):
        page = as_user(student_user).get(reverse("shift_swap", args=[mine.pk])).content.decode()
    assert "Ask Casey to swap" in page
    ask_swap(as_user(student_user), mine, casey_shift)
    ex = ShiftExchange.objects.get()
    assert ex.kind == "swap" and ex.status == ExchangeStatus.ASKED and ex.taker == casey
    asked = Notification.objects.get(user=casey_user, category="exchange")
    assert "asked to swap" in asked.body and "Avery" not in asked.body  # no co-workers' names
    with mock.patch("django.utils.timezone.localdate", return_value=START):
        page = as_user(casey_user).get(reverse("shift_exchange")).content.decode()
    assert "Avery asks to swap" in page
    post(as_user(casey_user), action="accept_swap", exchange=ex.pk)
    ex.refresh_from_db()
    assert ex.status == ExchangeStatus.PENDING
    mine.refresh_from_db()
    assert mine.employee == employee  # nothing moves before a supervisor decides
    sup = as_user(supervisor)
    assert "Swap: Casey" in sup.get(reverse("review_queue")).content.decode()
    resp = sup.post(reverse("review_queue"), {"action": "approve_exchange", "exchange": ex.pk}, follow=True)
    assert "Validation:" in resp.content.decode()
    mine.refresh_from_db()
    casey_shift.refresh_from_db()
    assert mine.employee == casey and casey_shift.employee == employee
    told = Notification.objects.filter(category="exchange").exclude(pk=asked.pk)
    assert {n.user for n in told} == {student_user, casey_user}
    assert all("approved" in n.body and "once a supervisor publishes" in n.body for n in told)


def test_swap_declined_changes_nothing(as_user, student_user, casey_user, employee, setup, casey_shift):
    _, _, mine = setup
    ask_swap(as_user(student_user), mine, casey_shift)
    ex = ShiftExchange.objects.get()
    post(as_user(casey_user), action="decline_swap", exchange=ex.pk)
    ex.refresh_from_db()
    mine.refresh_from_db()
    assert ex.status == ExchangeStatus.DECLINED and mine.employee == employee
    assert Notification.objects.filter(user=student_user, body__contains="declined").exists()


def test_only_the_co_worker_answers_a_swap(as_user, student_user, casey_user, lead_user, setup, casey_shift):
    _, _, mine = setup
    ask_swap(as_user(student_user), mine, casey_shift)
    ex = ShiftExchange.objects.get()
    for user in (student_user, lead_user):
        resp = post(as_user(user), action="accept_swap", exchange=ex.pk)
        assert "isn&#x27;t a swap request for you" in resp.content.decode()
    ex.refresh_from_db()
    assert ex.status == ExchangeStatus.ASKED


def test_a_shift_in_a_swap_cannot_also_be_offered(as_user, student_user, casey_user, setup, casey_shift):
    _, _, mine = setup
    ask_swap(as_user(student_user), mine, casey_shift)
    resp = post(as_user(student_user), action="offer", shift=mine.pk)
    assert "already offered or in a swap request" in resp.content.decode()
    resp = post(as_user(casey_user), action="offer", shift=casey_shift.pk)
    assert "already offered or in a swap request" in resp.content.decode()


def test_swap_with_an_open_shift_is_refused(as_user, student_user, setup):
    _, _, mine = setup
    resp = ask_swap(as_user(student_user), mine, open_shift())
    assert "isn&#x27;t a co-worker&#x27;s shift" in resp.content.decode()
    assert not ShiftExchange.objects.exists()


def test_staff_outside_the_pool_may_claim_open_shifts(as_user, student_user, supervisor, employee, setup):
    employee.employment_type = EmploymentType.NON_REGULAR
    employee.save()
    shift = open_shift()
    post(as_user(student_user), action="claim", shift=shift.pk)
    ex = ShiftExchange.objects.get()
    page = as_user(supervisor).get(reverse("review_queue")).content.decode()
    assert "Passes" in page and "scheduling pool" not in page
    as_user(supervisor).post(reverse("review_queue"), {"action": "approve_exchange", "exchange": ex.pk})
    shift.refresh_from_db()
    assert shift.employee == employee
