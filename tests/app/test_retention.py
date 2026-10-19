# SPDX-License-Identifier: AGPL-3.0-or-later
"""Retention settings per data type (SPEC §11): off until set, previewed, purged with their
audit snapshots, administrators only."""

from __future__ import annotations

from datetime import date, timedelta
from io import StringIO

import pytest
from django.apps import apps
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from app.availability.models import Availability, AvailabilityBlock, TimeOff
from app.core import retention
from app.core.models import Term
from app.imports.models import BatchKind, BatchStatus, ImportBatch
from app.imports.services import can_undo
from app.scheduling.models import Notification, SchedulePeriod, SolveRun

pytestmark = pytest.mark.django_db
TODAY = date(2028, 6, 1)


@pytest.fixture
def data(org, location, employee, student_user):
    old = Term.objects.create(
        organization=org, name="Spring 2027", start_date=date(2027, 1, 11), end_date=date(2027, 5, 7)
    )
    new = Term.objects.create(
        organization=org, name="Spring 2028", start_date=date(2028, 1, 10), end_date=date(2028, 5, 5)
    )
    a_old = Availability.objects.create(organization=org, employee=employee, term=old)
    AvailabilityBlock.objects.create(
        organization=org, availability=a_old, weekday=0, kind="unavailable", start_min=540, end_min=600
    )
    a_new = Availability.objects.create(organization=org, employee=employee, term=new)
    t_old = TimeOff.objects.create(
        organization=org, employee=employee, start_date=date(2027, 2, 1), end_date=date(2027, 2, 2)
    )
    t_new = TimeOff.objects.create(
        organization=org, employee=employee, start_date=date(2028, 5, 20), end_date=date(2028, 5, 21)
    )
    batch = ImportBatch.objects.create(
        organization=org,
        kind=BatchKind.ROSTER,
        status=BatchStatus.APPLIED,
        applied_at=timezone.now(),
        payload=[{"email": "avery@example.edu"}],
        plan={"create": [{"name": "Avery"}]},
        undo={"created_employees": [1], "updated": {}},
    )
    ImportBatch.objects.filter(pk=batch.pk).update(created_at=timezone.now() - timedelta(days=4000))
    note = Notification.objects.create(
        organization=org, user=student_user, subject="Schedule", body="Your shifts"
    )
    Notification.objects.filter(pk=note.pk).update(created_at=timezone.now() - timedelta(days=4000))
    period = SchedulePeriod.objects.create(
        organization=org, location=location, start_date=date(2027, 1, 16), weeks=2
    )
    run = SolveRun.objects.create(
        organization=org, period=period, mode="rebalance", finished_at=timezone.now() - timedelta(days=4000)
    )
    return {"a_old": a_old, "a_new": a_new, "t_old": t_old, "t_new": t_new, "batch": batch, "run": run}


def set_policy(org, **days):
    policy = retention.policy_for(org)
    for k, v in days.items():
        setattr(policy, k, v)
    policy.save()


def test_nothing_is_purged_until_a_period_is_set(org, data):
    assert all(d.days is None and d.count == 0 for d in retention.preview(org, TODAY))
    assert retention.purge(org, TODAY) == {}
    assert Availability.objects.count() == 2 and TimeOff.objects.count() == 2
    assert retention.purge_all(TODAY) == {}


def test_preview_counts_without_removing(org, data):
    set_policy(org, availability_days=365, time_off_days=365)
    due = {d.label: d.count for d in retention.preview(org, TODAY)}
    assert due["Term availability"] == 1 and due["Time-off requests"] == 1
    assert Availability.objects.count() == 2


def test_purge_removes_expired_rows_and_their_audit_snapshots(org, data):
    set_policy(
        org,
        availability_days=365,
        time_off_days=365,
        import_days=30,
        notification_days=180,
        solve_run_days=90,
    )
    counts = retention.purge(org, TODAY)
    assert counts == {
        "Term availability": 1,
        "Time-off requests": 1,
        "Import data": 1,
        "In-app notifications": 1,
        "Solver runs": 1,
    }
    assert list(Availability.objects.values_list("pk", flat=True)) == [data["a_new"].pk]
    assert list(TimeOff.objects.values_list("pk", flat=True)) == [data["t_new"].pk]
    assert not Notification.objects.exists() and not SolveRun.objects.exists()
    batch = ImportBatch.objects.get()
    assert (batch.payload, batch.plan, batch.undo) == ([], {}, {}) and batch.filename == ""
    assert not can_undo(batch)  # its undo data is gone
    events = apps.get_model("availability", "AvailabilityEvent").objects
    assert not events.filter(pgh_obj_id=data["a_old"].pk).exists()
    assert events.filter(pgh_obj_id=data["a_new"].pk).exists()
    assert (
        not apps.get_model("availability", "AvailabilityBlockEvent")
        .objects.filter(availability_id=data["a_old"].pk)
        .exists()
    )
    assert (
        not apps.get_model("availability", "TimeOffEvent")
        .objects.filter(pgh_obj_id=data["t_old"].pk)
        .exists()
    )
    assert not apps.get_model("imports", "ImportBatchEvent").objects.exclude(payload=[]).exists()
    policy = retention.policy_for(org)
    assert policy.last_purge_counts == counts and policy.last_purged_at is not None


def test_daily_task_and_command(org, data):
    from app.core.tasks import retention_purge

    set_policy(org, notification_days=180)
    out = StringIO()
    call_command("purge_expired", "--dry-run", stdout=out)
    assert "In-app notifications: 1 due" in out.getvalue()
    assert Notification.objects.exists()
    retention_purge(timestamp=0)
    assert not Notification.objects.exists()


def test_only_admins_manage_retention(as_user, admin_user, supervisor, org, data):
    url = reverse("retention")
    assert as_user(supervisor).get(url).status_code == 403
    client = as_user(admin_user)
    assert client.get(url).status_code == 200
    client.post(url, {"action": "save", "notification_days": "180", "availability_days": ""})
    assert retention.policy_for(org).notification_days == 180
    resp = client.post(url, {"action": "purge"}, follow=True)
    assert "Tick the confirmation box" in resp.content.decode() and Notification.objects.exists()
    resp = client.post(url, {"action": "purge", "confirm": "yes"}, follow=True)
    assert "In-app notifications: 1" in resp.content.decode()
    assert not Notification.objects.exists()


def test_periods_must_be_positive(as_user, admin_user, org, data):
    resp = as_user(admin_user).post(reverse("retention"), {"action": "save", "notification_days": "0"})
    assert resp.status_code == 200 and retention.policy_for(org).notification_days is None


def test_suggested_periods_fill_the_form_but_save_nothing(as_user, admin_user, org, data):
    client = as_user(admin_user)
    page = client.get(reverse("retention")).content.decode()
    assert "Suggested: 365." in page and "Suggested: 30." in page
    resp = client.post(reverse("retention"), {"action": "suggest"})
    html = resp.content.decode()
    assert resp.status_code == 200 and "Review them, then save" in html
    assert 'name="availability_days" value="365"' in html
    assert retention.policy_for(org).availability_days is None  # not saved
    assert Notification.objects.exists()  # and nothing purged
