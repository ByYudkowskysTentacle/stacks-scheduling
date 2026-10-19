# SPDX-License-Identifier: AGPL-3.0-or-later
"""The development demo can be recreated with --reset even after people have used it."""

from __future__ import annotations

from datetime import date
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import override_settings

from app.availability.models import TimeOff
from app.core.models import Organization
from app.people.models import Employee
from app.scheduling.models import ExchangeKind, ExchangeStatus, Shift, ShiftExchange

pytestmark = pytest.mark.django_db


@override_settings(DEBUG=True)
def test_reset_removes_everything_the_demo_grew():
    call_command("seed_demo", stdout=StringIO())
    org = Organization.objects.get(name="Demo Library")
    emp = Employee.objects.filter(organization=org).first()
    TimeOff.objects.create(
        organization=org, employee=emp, start_date=date(2027, 1, 18), end_date=date(2027, 1, 18)
    )
    shift = Shift.objects.filter(organization=org).first()
    ShiftExchange.objects.create(
        organization=org, shift=shift, kind=ExchangeKind.COVER, status=ExchangeStatus.OPEN, requester=emp
    )
    call_command("seed_demo", "--reset", stdout=StringIO())
    assert Organization.objects.filter(name="Demo Library").count() == 1
    assert not TimeOff.objects.exists() and not ShiftExchange.objects.exists()


def test_refuses_without_debug():
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("seed_demo", stdout=StringIO())
