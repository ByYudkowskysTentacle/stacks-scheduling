# SPDX-License-Identifier: AGPL-3.0-or-later
"""Employee self-service (SPEC §9.2): the private calendar feed and time-off requests."""
# ruff: noqa: F811 (the `setup` fixture is shared from test_published)

from __future__ import annotations

import re
from datetime import date
from unittest import mock

import pytest
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from app.availability.models import TimeOff, TimeOffStatus
from app.scheduling import services
from app.scheduling.models import CalendarFeed
from tests.app.test_published import MONDAY, START, setup  # noqa: F401

pytestmark = pytest.mark.django_db


def feed_url(client) -> str:
    page = client.post(reverse("calendar_feed_manage"), {"action": "create"}).content.decode()
    return re.search(r'value="(http[^"]+\.ics)"', page).group(1)


def fetch(client, url):
    with mock.patch("django.utils.timezone.localdate", return_value=START):
        return client.get(url.replace("http://testserver", ""))


# --- Calendar feed --------------------------------------------------------------------------


def test_feed_lists_own_published_shifts_in_utc(client, as_user, student_user, setup):
    url = feed_url(as_user(student_user))
    client.logout()
    resp = fetch(client, url)
    assert resp.status_code == 200 and resp["Content-Type"].startswith("text/calendar")
    body = resp.content.decode()
    assert "BEGIN:VCALENDAR" in body and body.count("BEGIN:VEVENT") == 1
    assert "DTSTART:20270118T150000Z" in body  # 9am Chicago in January
    assert "DTEND:20270118T180000Z" in body
    assert "With: Blake" in body and "Note: Bring your badge" in body
    assert "Blake's private note" not in body and "Fullname" not in body


def test_feed_shows_published_not_draft(client, as_user, student_user, setup):
    _, _, mine = setup
    url = feed_url(as_user(student_user))
    mine.start_min = 600
    mine.save()
    assert "DTSTART:20270118T150000Z" in fetch(client, url).content.decode()


def test_only_the_hash_is_stored_and_reset_kills_the_old_link(client, as_user, student_user, setup):
    student = as_user(student_user)
    old = feed_url(student)
    token = old.rsplit("/", 1)[1].removesuffix(".ics")
    with connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM scheduling_calendarfeed t WHERE t::text LIKE %s", [f"%{token}%"])
        assert cur.fetchone()[0] == 0
    new = feed_url(student)
    assert fetch(client, old).status_code == 404
    assert fetch(client, new).status_code == 200
    student.post(reverse("calendar_feed_manage"), {"action": "remove"})
    assert fetch(client, new).status_code == 404
    assert not CalendarFeed.objects.exists()


def test_archived_employees_feed_stops(client, as_user, student_user, employee, setup):
    url = feed_url(as_user(student_user))
    employee.archived_at = timezone.now()
    employee.save()
    assert fetch(client, url).status_code == 404


def test_unknown_token_is_not_found(client, db):
    assert client.get(reverse("calendar_feed", args=["nope"])).status_code == 404


# --- Time off -------------------------------------------------------------------------------


def request_off(client, **data):
    with mock.patch("django.utils.timezone.localdate", return_value=START):
        return client.post(reverse("my_time_off"), {"all_day": "on", **data}, follow=True)


def test_student_requests_and_cancels_time_off(as_user, student_user, employee, setup):
    client = as_user(student_user)
    resp = request_off(client, start_date="2027-01-18")
    assert "A supervisor will review it" in resp.content.decode()
    t = TimeOff.objects.get(employee=employee)
    assert t.status == TimeOffStatus.PENDING and t.all_day and t.end_date == MONDAY
    with mock.patch("django.utils.timezone.localdate", return_value=START):
        client.post(reverse("my_time_off"), {"action": "cancel", "request": t.pk})
    t.refresh_from_db()
    assert t.status == TimeOffStatus.CANCELLED


def test_time_off_in_the_past_or_back_to_front_is_refused(as_user, student_user, setup):
    client = as_user(student_user)
    assert "Choose today or a later date" in request_off(client, start_date="2027-01-01").content.decode()
    resp = request_off(client, start_date="2027-01-20", end_date="2027-01-19")
    assert "before the start date" in resp.content.decode()
    resp = request_off(client, start_date="2027-01-20", all_day="", start_min="13:00", end_min="9:00")
    assert "before the start time" in resp.content.decode()
    assert not TimeOff.objects.exists()


def test_supervisor_sees_clashes_then_approves(as_user, supervisor, employee, setup):
    period, _, mine = setup
    t = TimeOff.objects.create(
        organization=employee.organization, employee=employee, start_date=MONDAY, end_date=MONDAY
    )
    client = as_user(supervisor)
    page = client.get(reverse("time_off_review")).content.decode()
    assert "Clashes with 1 shift" in page and "Who could take it?" in page
    assert "Published" in page
    before = services.validate_period(period)
    resp = client.post(reverse("time_off_review"), {"request": t.pk, "action": "approve"}, follow=True)
    t.refresh_from_db()
    assert t.status == TimeOffStatus.APPROVED and t.decided_by == supervisor
    assert "1 of their shifts now clashes" in resp.content.decode()
    mine.refresh_from_db()
    assert mine.employee == employee  # approving changes no shifts
    after = services.validate_period(period)
    new = {i.message for i in after.issues} - {i.message for i in before.issues}
    assert any("approved time off" in m for m in new)


def test_decided_requests_cant_be_decided_again(as_user, supervisor, employee, setup):
    t = TimeOff.objects.create(
        organization=employee.organization,
        employee=employee,
        start_date=MONDAY,
        end_date=MONDAY,
        status=TimeOffStatus.DENIED,
    )
    resp = as_user(supervisor).post(
        reverse("time_off_review"), {"request": t.pk, "action": "approve"}, follow=True
    )
    t.refresh_from_db()
    assert t.status == TimeOffStatus.DENIED and "already been decided" in resp.content.decode()


def test_only_supervisors_review_time_off(as_user, student_user, lead_user, setup):
    for user in (student_user, lead_user):
        assert as_user(user).get(reverse("time_off_review")).status_code == 403


def test_time_off_requests_are_private_to_the_employee(as_user, lead_user, employee, setup):
    TimeOff.objects.create(
        organization=employee.organization, employee=employee, start_date=MONDAY, end_date=MONDAY
    )
    page = as_user(lead_user).get(reverse("my_time_off")).content.decode()
    assert "No requests yet" in page


def test_unused_date_import_guard():
    assert date(2027, 1, 18) == MONDAY
