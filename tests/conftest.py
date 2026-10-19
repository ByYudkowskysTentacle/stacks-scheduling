# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared fixtures for app tests. All data is synthetic (CLAUDE.md §2.1)."""

from __future__ import annotations

from datetime import date

import pytest

from app.accounts.models import User
from app.core.models import Closure, Location, Membership, OpenInterval, OperatingHoursSet, Organization, Role
from app.people.models import Employee, PayRole
from domain.calendar import FRI, MON, SAT, SUN, THU, TUE, WED

PASSWORD = "a-long-test-password"


@pytest.fixture
def org(db):
    return Organization.objects.create(name="Test Library")


@pytest.fixture
def other_org(db):
    return Organization.objects.create(name="Other Library")


@pytest.fixture
def location(org):
    loc = Location.objects.create(organization=org, name="Main")
    hours = OperatingHoursSet.objects.create(
        organization=org, location=loc, name="Term", effective_from=date(2026, 1, 1)
    )
    pattern = {
        **{wd: (450, 1320) for wd in (MON, TUE, WED, THU)},
        FRI: (450, 960),
        SAT: (720, 960),
        SUN: (1080, 1320),
    }
    for wd, (s, e) in pattern.items():
        OpenInterval.objects.create(organization=org, hours_set=hours, weekday=wd, start_min=s, end_min=e)
    Closure.objects.create(
        organization=org,
        location=loc,
        reason="Chapel",
        kind=Closure.RECURRING,
        weekday=WED,
        start_min=600,
        end_min=660,
    )
    return loc


@pytest.fixture
def pay_role(org):
    return PayRole.objects.create(organization=org, name="Student Assistant")


def make_user(org, email, role):
    user = User.objects.create_user(email, PASSWORD)
    Membership.objects.create(user=user, organization=org, role=role)
    return user


@pytest.fixture
def admin_user(org):
    return make_user(org, "admin@example.edu", Role.ADMIN)


@pytest.fixture
def supervisor(org):
    return make_user(org, "supervisor@example.edu", Role.SUPERVISOR)


@pytest.fixture
def employee(org, pay_role):
    return Employee.objects.create(
        organization=org,
        full_name="Avery Example",
        preferred_name="Avery",
        email="avery@example.edu",
        default_pay_role=pay_role,
    )


@pytest.fixture
def student_user(org, employee):
    user = make_user(org, "avery@example.edu", Role.EMPLOYEE)
    employee.user = user
    employee.save()
    return user


@pytest.fixture
def lead_user(org):
    user = make_user(org, "lead@example.edu", Role.EMPLOYEE)
    Employee.objects.create(
        organization=org, full_name="Blake Sample", preferred_name="Blake", user=user, shift_lead_access=True
    )
    return user


@pytest.fixture
def as_user(client):
    def login(user):
        client.force_login(user)
        return client

    return login
