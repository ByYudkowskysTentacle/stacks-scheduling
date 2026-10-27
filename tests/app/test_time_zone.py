# SPDX-License-Identifier: AGPL-3.0-or-later
"""The organization's time zone (D119): a setting chosen from the IANA list, used by every
request, the calendar feed, the freeze window and the nightly jobs, and changed only with a
confirmation and an audit record. One failing and one passing case per rule (CLAUDE.md §5)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from importlib import import_module
from io import StringIO
from unittest import mock

import pytest
from django.apps import apps
from django.contrib.sessions.backends.db import SessionStore
from django.core.management import CommandError, call_command
from django.test import RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone

from app.core import retention
from app.core.middleware import OrganizationMiddleware
from app.core.models import Organization
from app.core.timezones import local_today, time_zone_choices
from app.scheduling import feeds, services, solver
from app.scheduling.models import Notification, SchedulePeriod, Shift

pytestmark = pytest.mark.django_db
# 02:00 UTC on Mon 18 Jan 2027: still Sunday evening in Chicago, Monday morning in Tokyo.
NOW = datetime(2027, 1, 18, 2, 0, tzinfo=UTC)


def at(moment: datetime):
    return mock.patch("django.utils.timezone.now", return_value=moment)


def in_zone(org, name: str):
    org.time_zone = name
    org.save()
    return org


# --- The setting -----------------------------------------------------------------------------


@override_settings(TIME_ZONE="Europe/Dublin")
def test_a_new_organization_starts_in_the_installations_zone():
    assert Organization.objects.create(name="New Library").time_zone == "Europe/Dublin"


def test_the_migration_moves_existing_organizations_to_the_installations_zone(org):
    in_zone(org, "Asia/Tokyo")
    migration = import_module("app.core.migrations.0010_organization_time_zone")
    with override_settings(TIME_ZONE="America/Denver"):
        migration.use_installation_zone(apps, None)
    org.refresh_from_db()
    assert org.time_zone == "America/Denver"


def test_the_settings_page_offers_the_iana_zones_by_region(as_user, admin_user):
    page = as_user(admin_user).get(reverse("organization_settings")).content.decode()
    assert (
        '<optgroup label="America">' in page
        and '<option value="Europe/Dublin">Europe/Dublin</option>' in page
    )
    assert 'value="Etc/GMT+6"' not in page  # legacy aliases aren't offered


def test_a_legacy_alias_already_stored_stays_selectable():
    choices = dict(time_zone_choices("Etc/GMT+6"))
    assert choices["Other"] == [("Etc/GMT+6", "Etc/GMT+6")]
    assert "Other" not in dict(time_zone_choices("America/Chicago"))


def _settings_post(org, **changes):
    data = {"name": org.name, "time_zone": org.time_zone, "week_start": org.week_start}
    data["planning_period_weeks"] = org.planning_period_weeks
    return {**data, **changes}


def test_an_unknown_zone_is_refused(as_user, admin_user, org):
    in_zone(org, "America/Chicago")
    response = as_user(admin_user).post(
        reverse("organization_settings"), _settings_post(org, time_zone="Mars/Olympus")
    )
    assert "Select a valid choice" in response.content.decode()
    org.refresh_from_db()
    assert org.time_zone == "America/Chicago"


def test_changing_the_zone_asks_for_confirmation_first(as_user, admin_user, org, location, employee):
    in_zone(org, "America/Chicago")
    period = SchedulePeriod.objects.create(
        organization=org, location=location, start_date=date(2027, 1, 16), weeks=2
    )
    Shift.objects.create(
        organization=org,
        location=location,
        period=period,
        employee=employee,
        date=date(2027, 1, 18),
        start_min=540,
        end_min=780,
    )
    services.publish(period, admin_user)
    with at(datetime(2027, 1, 16, 15, 0, tzinfo=UTC)):
        response = as_user(admin_user).post(
            reverse("organization_settings"), _settings_post(org, time_zone="Asia/Tokyo")
        )
    page = response.content.decode()
    assert response.status_code == 200 and "Check before changing the time zone" in page
    assert (
        "(now Sat 16 Jan, 9:00 a.m.)" in page and "(now Sun 17 Jan, 12:00 a.m.)" in page
    )  # each zone's own time
    assert (
        "This affects 1 upcoming shift and 1 published schedule that haven't ended" in page
        and "a 9am shift stays 9am" in page
    )
    org.refresh_from_db()
    assert org.time_zone == "America/Chicago"


def test_a_confirmed_zone_change_is_saved_and_audited(as_user, admin_user, org):
    in_zone(org, "America/Chicago")
    data = _settings_post(org, time_zone="Asia/Tokyo", confirmed_zone="Asia/Tokyo", confirm_time_zone="yes")
    response = as_user(admin_user).post(reverse("organization_settings"), data)
    assert response.status_code == 302
    org.refresh_from_db()
    assert org.time_zone == "Asia/Tokyo"
    event = Organization.pgh_event_model.objects.filter(pgh_obj=org).order_by("-pgh_id").first()
    assert event.pgh_label == "update" and event.time_zone == "Asia/Tokyo"
    assert event.pgh_context is not None and event.pgh_context.metadata.get("user") == admin_user.pk


def test_saving_the_check_without_the_tick_says_what_is_missing(as_user, admin_user, org):
    in_zone(org, "America/Chicago")
    data = _settings_post(org, time_zone="Asia/Tokyo", confirmed_zone="Asia/Tokyo")
    page = as_user(admin_user).post(reverse("organization_settings"), data).content.decode()
    assert "Tick the box to change the time zone." in page
    assert 'aria-invalid="true" aria-describedby="confirm_time_zone_error"' in page
    org.refresh_from_db()
    assert org.time_zone == "America/Chicago"


def test_a_tick_only_confirms_the_zone_that_was_checked(as_user, admin_user, org):
    """Check Denver, then switch the select to Honolulu and tick: Honolulu gets its own check."""
    in_zone(org, "America/Chicago")
    data = _settings_post(
        org, time_zone="Pacific/Honolulu", confirmed_zone="America/Denver", confirm_time_zone="yes"
    )
    page = as_user(admin_user).post(reverse("organization_settings"), data).content.decode()
    assert "You chose a different zone after the check, so check Pacific/Honolulu" in page
    assert 'name="confirmed_zone" value="Pacific/Honolulu"' in page
    assert "Tick the box" not in page
    org.refresh_from_db()
    assert org.time_zone == "America/Chicago"


def test_other_settings_save_without_the_zone_confirmation(as_user, admin_user, org):
    in_zone(org, "America/Chicago")
    response = as_user(admin_user).post(
        reverse("organization_settings"), _settings_post(org, name="Renamed Library")
    )
    assert response.status_code == 302
    org.refresh_from_db()
    assert org.name == "Renamed Library" and org.time_zone == "America/Chicago"


def test_bootstrap_refuses_an_unknown_zone(monkeypatch):
    monkeypatch.setenv("STACKS_ADMIN_PASSWORD", "a-long-test-password")
    with pytest.raises(CommandError, match="Unknown time zone"):
        call_command(
            "bootstrap",
            "--organization",
            "Test Library",
            "--admin-email",
            "boot@example.edu",
            "--time-zone",
            "Mars/Olympus",
        )


@override_settings(TIME_ZONE="Pacific/Auckland")
def test_bootstrap_defaults_to_the_installations_zone(monkeypatch):
    monkeypatch.setenv("STACKS_ADMIN_PASSWORD", "a-long-test-password")
    call_command(
        "bootstrap", "--organization", "Test Library", "--admin-email", "boot@example.edu", stdout=StringIO()
    )
    assert Organization.objects.get(name="Test Library").time_zone == "Pacific/Auckland"


# --- Where it applies ------------------------------------------------------------------------


def _through_middleware(user):
    request = RequestFactory().get("/")
    request.user = user
    request.session = SessionStore()
    seen = {}

    def view(req):
        seen["zone"] = timezone.get_current_timezone_name()
        seen["today"] = timezone.localdate()
        return None

    with at(NOW):
        OrganizationMiddleware(view)(request)
    return seen


@override_settings(TIME_ZONE="America/Chicago")
def test_a_request_runs_in_the_organizations_zone(org, supervisor):
    in_zone(org, "Asia/Tokyo")
    assert _through_middleware(supervisor) == {"zone": "Asia/Tokyo", "today": date(2027, 1, 18)}
    assert timezone.get_current_timezone_name() == "America/Chicago"  # nothing carries over


@override_settings(TIME_ZONE="America/Chicago")
def test_a_request_without_an_organization_uses_the_server_default(django_user_model):
    user = django_user_model.objects.create_user("nobody@example.edu", "a-long-test-password")
    assert _through_middleware(user) == {"zone": "America/Chicago", "today": date(2027, 1, 17)}


def test_today_follows_the_organizations_zone(org):
    with at(NOW):
        assert local_today(in_zone(org, "Asia/Tokyo")) == date(2027, 1, 18)
        assert local_today(in_zone(org, "America/Chicago")) == date(2027, 1, 17)


def _published_shift(org, location, employee, supervisor):
    period = SchedulePeriod.objects.create(
        organization=org, location=location, start_date=date(2027, 1, 16), weeks=2
    )
    Shift.objects.create(
        organization=org,
        location=location,
        period=period,
        employee=employee,
        date=date(2027, 1, 18),
        start_min=540,
        end_min=780,
    )
    services.publish(period, supervisor)


def test_the_calendar_feed_places_shifts_in_the_organizations_zone(org, location, employee, supervisor):
    _published_shift(org, location, employee, supervisor)
    in_zone(org, "Asia/Tokyo")
    ics = feeds.build_ics(employee, today=date(2027, 1, 17))
    assert "DTSTART:20270118T000000Z" in ics and "DTEND:20270118T040000Z" in ics  # 9am-1pm in Tokyo
    in_zone(org, "America/Chicago")
    ics = feeds.build_ics(employee, today=date(2027, 1, 17))
    assert "DTSTART:20270118T150000Z" in ics  # the same 9am, wall clock, in Chicago


def test_the_freeze_window_counts_from_the_organizations_now(org):
    with at(NOW):
        assert solver._freeze_until(in_zone(org, "Asia/Tokyo"), 2) == (date(2027, 1, 18), 13 * 60)
        assert solver._freeze_until(in_zone(org, "America/Chicago"), 2) == (date(2027, 1, 17), 22 * 60)


def test_the_retention_purge_cuts_off_at_the_organizations_midnight(org, student_user):
    retention.policy_for(org).__class__.objects.filter(organization=org).update(notification_days=1)
    note = Notification.objects.create(
        organization=org, user=student_user, subject="Schedule", body="Your shifts"
    )
    # Sent at noon UTC on Sat 16 Jan: Saturday evening in Tokyo, Saturday morning in Chicago.
    Notification.objects.filter(pk=note.pk).update(created_at=datetime(2027, 1, 16, 12, 0, tzinfo=UTC))
    label = "In-app notifications"
    with at(NOW):  # Tokyo is on Monday, so Saturday is more than a day ago
        assert {d.label: d.count for d in retention.preview(in_zone(org, "Asia/Tokyo"))}[label] == 1
        assert {d.label: d.count for d in retention.preview(in_zone(org, "America/Chicago"))}[label] == 0
        assert retention.purge(org)[label] == 0  # Chicago: still Sunday, so nothing is due yet
    assert Notification.objects.filter(pk=note.pk).exists()
