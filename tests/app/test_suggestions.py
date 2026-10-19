# SPDX-License-Identifier: AGPL-3.0-or-later
"""Suggestion queries one click from the schedule (SPEC §7.5) and remove-from-pool (scenario 9,
SPEC §9.4). All data is synthetic."""

from __future__ import annotations

from datetime import date

import pytest
from django.core import mail
from django.urls import reverse

from app.availability.models import Availability, AvailabilityBlock
from app.core.models import Term
from app.people.models import Employee, PayRole
from app.scheduling.models import Notification, SchedulePeriod, Shift
from tests.app.test_foundations import employee_post_data

pytestmark = pytest.mark.django_db
START = date(2027, 1, 16)  # a Saturday, after "today" in these tests
MONDAY = date(2027, 1, 18)
TUESDAY = date(2027, 1, 19)
FRIDAY = date(2027, 1, 22)


@pytest.fixture
def setup(org, location, supervisor, employee, pay_role):
    term = Term.objects.create(
        organization=org, name="Spring 2027", start_date=date(2027, 1, 11), end_date=date(2027, 5, 7)
    )
    desk = PayRole.objects.create(organization=org, name="Desk", color="#aa5500")
    people = {
        name: Employee.objects.create(
            organization=org, full_name=f"{name} Sample", preferred_name=name, default_pay_role=desk
        )
        for name in ("Blake", "Casey", "Drew")
    }
    people["Avery"] = employee
    for e in people.values():
        Availability.objects.create(organization=org, employee=e, term=term)
    # Drew has class Monday 9-11.
    AvailabilityBlock.objects.create(
        organization=org,
        availability=Availability.objects.get(employee=people["Drew"]),
        weekday=0,
        kind="unavailable",
        start_min=540,
        end_min=660,
    )
    period = SchedulePeriod.objects.create(organization=org, location=location, start_date=START, weeks=1)

    def shift(who, day, start, end):
        return Shift.objects.create(
            organization=org,
            location=location,
            period=period,
            employee=who,
            date=day,
            start_min=start,
            end_min=end,
            pay_role=pay_role,
        )

    shifts = [
        shift(employee, MONDAY, 540, 780),
        shift(employee, TUESDAY, 540, 780),
        shift(employee, FRIDAY, 600, 840),
        shift(people["Blake"], TUESDAY, 1080, 1320),
    ]
    return {"period": period, "people": people, "shifts": shifts, "desk": desk}


def test_who_could_take_lists_eligible_and_blocked(as_user, supervisor, setup):
    period, monday = setup["period"], setup["shifts"][0]
    page = as_user(supervisor).get(reverse("shift_takers", args=[period.pk, monday.pk]))
    assert page.status_code == 200
    text = page.content.decode()
    assert "Give it to Blake" in text and "Give it to Casey" in text
    assert "Give it to Drew" not in text
    assert "Drew is unavailable Mon 18 Jan 9am–11am" in text


def test_giving_a_shift_takes_the_new_persons_pay_role_and_notifies_nobody(as_user, supervisor, setup):
    period, monday = setup["period"], setup["shifts"][0]
    casey = setup["people"]["Casey"]
    resp = as_user(supervisor).post(
        reverse("shift_takers", args=[period.pk, monday.pk]), {"employee": casey.pk}, follow=True
    )
    monday.refresh_from_db()
    assert monday.employee == casey and monday.pay_role == setup["desk"]  # scenario 15
    assert "Validation:" in resp.content.decode()
    assert mail.outbox == [] and not Notification.objects.exists()


def test_an_ineligible_choice_is_refused(as_user, supervisor, setup):
    period, monday = setup["period"], setup["shifts"][0]
    drew = setup["people"]["Drew"]
    resp = as_user(supervisor).post(
        reverse("shift_takers", args=[period.pk, monday.pk]), {"employee": drew.pk}, follow=True
    )
    monday.refresh_from_db()
    assert monday.employee == setup["people"]["Avery"]
    assert "can&#x27;t take this shift" in resp.content.decode()


def test_cover_a_gap_adds_a_draft_shift(as_user, supervisor, setup):
    period, casey = setup["period"], setup["people"]["Casey"]
    url = reverse("gap_takers", args=[period.pk])
    query = {"date": "2027-01-20", "start": "1200", "end": "1230"}  # 30 minutes before close
    page = as_user(supervisor).get(url, query)
    assert page.status_code == 200
    assert "shorter than the minimum shift length" in page.content.decode()
    as_user(supervisor).post(
        url, {"date": "2027-01-20", "start": "1200", "end": "1320", "employee": casey.pk}
    )
    added = Shift.objects.get(employee=casey, date=date(2027, 1, 20))
    assert added.interval.label() == "8pm–10pm" and added.pay_role == setup["desk"]


def test_gap_outside_the_period_is_not_found(as_user, supervisor, setup):
    url = reverse("gap_takers", args=[setup["period"].pk])
    assert (
        as_user(supervisor).get(url, {"date": "2030-01-01", "start": "540", "end": "660"}).status_code == 404
    )


def test_raise_hours_lists_takeable_shifts(as_user, supervisor, setup):
    period, casey = setup["period"], setup["people"]["Casey"]
    client = as_user(supervisor)
    page = client.get(reverse("raise_hours", args=[period.pk, casey.pk]))
    assert page.status_code == 200
    text = page.content.decode()
    assert "How can I raise Casey's hours?" in text
    assert "Tue 19 Jan 9am–1pm" in text and "Avery" in text
    blake_evening = setup["shifts"][3]
    client.post(reverse("raise_hours", args=[period.pk, casey.pk]), {"shift": blake_evening.pk})
    blake_evening.refresh_from_db()
    assert blake_evening.employee == casey


def test_only_supervisors_use_suggestions(as_user, lead_user, student_user, setup):
    period, monday = setup["period"], setup["shifts"][0]
    for user in (lead_user, student_user):
        client = as_user(user)
        assert client.get(reverse("shift_takers", args=[period.pk, monday.pk])).status_code == 403
        assert client.get(reverse("raise_hours", args=[period.pk, monday.employee_id])).status_code == 403


# --- Remove from pool (scenario 9) ----------------------------------------------------------


def test_remove_from_pool_shows_the_plan_then_reassigns_without_deleting(as_user, supervisor, setup):
    avery = setup["people"]["Avery"]
    client = as_user(supervisor)
    url = reverse("remove_from_pool", args=[avery.pk])
    page = client.get(url)
    assert page.status_code == 200
    assert page.content.decode().count('<tr><th scope="row">') == 3
    avery.refresh_from_db()
    assert avery.in_scheduling_pool  # nothing changes until confirmed
    count = Shift.objects.count()
    resp = client.post(url, {"confirm": "yes"}, follow=True)
    avery.refresh_from_db()
    assert not avery.in_scheduling_pool
    assert Shift.objects.count() == count and not Shift.objects.filter(deleted_at__isnull=False).exists()
    moved = [Shift.objects.get(pk=s.pk) for s in setup["shifts"][:3]]
    assert all(s.employee not in (None, avery) for s in moved)
    assert [(s.date, s.interval) for s in moved] == [(s.date, s.interval) for s in setup["shifts"][:3]]
    assert "validation" in resp.content.decode().lower()


def test_remove_from_pool_needs_confirmation(as_user, supervisor, setup):
    avery = setup["people"]["Avery"]
    as_user(supervisor).post(reverse("remove_from_pool", args=[avery.pk]), {})
    avery.refresh_from_db()
    assert avery.in_scheduling_pool
    assert Shift.objects.filter(employee=avery).count() == 3


def test_unticking_the_pool_box_offers_reassignment(as_user, supervisor, setup):
    avery = setup["people"]["Avery"]
    data = employee_post_data(full_name=avery.full_name, preferred_name="Avery", email=avery.email)
    del data["in_scheduling_pool"]
    resp = as_user(supervisor).post(reverse("employee_edit", args=[avery.pk]), data)
    assert resp.status_code == 302 and resp.url == reverse("remove_from_pool", args=[avery.pk])
    assert Shift.objects.filter(employee=avery).count() == 3  # offered, not done


def test_archiving_someone_with_upcoming_shifts_offers_reassignment(as_user, supervisor, setup):
    avery = setup["people"]["Avery"]
    client = as_user(supervisor)
    assert "3 upcoming shifts" in client.get(reverse("employee_archive", args=[avery.pk])).content.decode()
    resp = client.post(reverse("employee_archive", args=[avery.pk]), {"confirm": "yes"})
    assert resp.url == reverse("remove_from_pool", args=[avery.pk])
