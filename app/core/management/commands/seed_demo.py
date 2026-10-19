# SPDX-License-Identifier: AGPL-3.0-or-later
"""Load a synthetic, pilot-shaped demo organization for development (CLAUDE.md §2.1).

Every name and address is made up; emails use example.edu. Refuses to run unless
DJANGO_DEBUG is on, so it can't touch a production database by accident.

    python manage.py seed_demo            # creates "Demo Library" if missing
    python manage.py seed_demo --reset    # deletes and recreates the demo organization
"""

from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal

import pghistory
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from app.accounts.models import User
from app.availability.models import Availability, AvailabilityBlock, AvailabilityReminder, Status, TimeOff
from app.core.models import (
    Closure,
    Location,
    Membership,
    OpenInterval,
    OperatingHoursSet,
    Organization,
    RetentionPolicy,
    Role,
    Term,
)
from app.imports.models import ImportBatch
from app.people.models import Employee, EmployeePayRole, EmployeeTag, EmploymentType, PayRole, Tag
from app.scheduling.models import (
    CalendarFeed,
    CallOut,
    CoverageOverride,
    DesiredHours,
    DutyAssignment,
    DutyKind,
    HourCapRule,
    HourTargetRule,
    Notification,
    PublishedVersion,
    RotationAssignment,
    SchedulePeriod,
    SchedulingRules,
    Shift,
    ShiftExchange,
    SolveRun,
    TagCoverageRule,
)
from domain.availability import normalise_blocks
from domain.calendar import FRI, MON, SAT, SUN, THU, TUE, WED
from fixtures import synthetic

DEMO_ORG = "Demo Library"
DEMO_PASSWORD = "demo-password-123"  # noqa: S105 (development-only demo accounts)
FIRST = [
    "Avery",
    "Blake",
    "Casey",
    "Devon",
    "Emery",
    "Finley",
    "Gray",
    "Harper",
    "Indigo",
    "Jordan",
    "Kai",
    "Logan",
    "Morgan",
    "Nico",
    "Oakley",
    "Parker",
    "Quinn",
    "Reese",
    "Sage",
    "Taylor",
    "Umi",
    "Val",
    "Wren",
    "Xen",
    "Yael",
    "Zion",
    "Arden",
    "Bellamy",
    "Cypress",
    "Dakota",
    "Ellis",
    "Frankie",
]
LAST = ["Example", "Sample", "Placeholder", "Testcase", "Fixture", "Synthetic", "Mockley", "Dummy"]
PILOT_HOURS = {
    **{wd: [(450, 1320)] for wd in (MON, TUE, WED, THU)},
    FRI: [(450, 960)],
    SAT: [(720, 960)],
    SUN: [(1080, 1320)],
}


class Command(BaseCommand):
    help = "Create a synthetic demo organization (development only)."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Delete the demo organization first.")
        parser.add_argument("--seed", type=int, default=1)
        parser.add_argument(
            "--generate",
            type=int,
            default=0,
            metavar="SECONDS",
            help="Fill the first period with a generated schedule, solving for this long, and publish it.",
        )

    def handle(self, *args, **opts):
        if not settings.DEBUG:
            raise CommandError("seed_demo only runs with DJANGO_DEBUG=1.")
        with transaction.atomic(), pghistory.context(command="seed_demo"):
            existing = Organization.objects.filter(name=DEMO_ORG).first()
            if existing and not opts["reset"]:
                self.stdout.write("Demo organization already exists; use --reset to recreate it.")
                return
            if existing:
                delete_organization(existing)
            org = build_demo(random.Random(opts["seed"]))  # noqa: S311 (synthetic data)
            if opts["generate"]:
                summary = generate_and_publish(org, opts["generate"])
                self.stdout.write(f"Generated and published the first period: {summary}.")
        self.stdout.write(
            self.style.SUCCESS(
                f"Created {org.name}. Sign in as supervisor@example.edu or admin@example.edu "
                f"with password {DEMO_PASSWORD!r}."
            )
        )


def delete_organization(org: Organization) -> None:
    """Development helper: remove every row of the demo organization, children first."""
    for model in (
        ShiftExchange,
        CalendarFeed,
        TimeOff,
        AvailabilityReminder,
        RetentionPolicy,
        SolveRun,
        DesiredHours,
        HourTargetRule,
        RotationAssignment,
        DutyAssignment,
        Notification,
        CallOut,
        PublishedVersion,
        Shift,
        SchedulePeriod,
        TagCoverageRule,
        HourCapRule,
        CoverageOverride,
        SchedulingRules,
        ImportBatch,
        AvailabilityBlock,
        Availability,
        EmployeeTag,
        EmployeePayRole,
        Employee,
        Tag,
        PayRole,
        OpenInterval,
        OperatingHoursSet,
        Closure,
        Term,
    ):
        model.objects.filter(organization=org).delete()
    Location.objects.filter(organization=org).delete()
    Membership.objects.filter(organization=org).delete()
    org.delete()


def build_demo(rng: random.Random) -> Organization:
    org = Organization.objects.create(name=DEMO_ORG)
    loc = Location.objects.create(organization=org, name="Main library")
    hours = OperatingHoursSet.objects.create(
        organization=org, location=loc, name="Regular term hours", effective_from=date(2026, 8, 1)
    )
    OpenInterval.objects.bulk_create(
        OpenInterval(organization=org, hours_set=hours, weekday=wd, start_min=s, end_min=e)
        for wd, ivs in PILOT_HOURS.items()
        for s, e in ivs
    )
    Closure.objects.create(
        organization=org,
        location=loc,
        reason="Chapel",
        kind=Closure.RECURRING,
        weekday=WED,
        start_min=600,
        end_min=660,
    )
    fall = Term.objects.create(
        organization=org, name="Fall 2026", start_date=date(2026, 8, 24), end_date=date(2026, 12, 11)
    )
    spring = Term.objects.create(
        organization=org, name="Spring 2027", start_date=date(2027, 1, 11), end_date=date(2027, 5, 7)
    )

    assistant = PayRole.objects.create(organization=org, name="Student Assistant", color="#1f6f8b")
    fws = PayRole.objects.create(organization=org, name="FWS Student Assistant", color="#7a3e9d")
    tags = {
        name: Tag.objects.create(organization=org, name=name)
        for name in ("Shift lead", "Opener", "Cataloguer", "Processor", "Marketing", "New employee")
    }

    for email, role in (("admin@example.edu", Role.ADMIN), ("supervisor@example.edu", Role.SUPERVISOR)):
        user = User.objects.filter(email=email).first() or User.objects.create_user(email, DEMO_PASSWORD)
        Membership.objects.create(user=user, organization=org, role=role)

    names = [(first, rng.choice(LAST)) for first in rng.sample(FIRST, 32)]
    for i, (first, last) in enumerate(names, start=1):
        regular = i <= 28
        pay_role = fws if i % 3 == 0 else assistant
        emp = Employee.objects.create(
            organization=org,
            full_name=f"{first} {last}",
            preferred_name=first,
            email=f"student{i:02d}@example.edu",
            employment_type=EmploymentType.REGULAR if regular else EmploymentType.NON_REGULAR,
            hire_date=date(2025, 8, 20),
            new_employee_until=date(2027, 2, 28) if 5 <= i <= 8 else None,
            default_pay_role=pay_role,
            default_wage=Decimal("12.50") if pay_role is assistant else Decimal("13.00"),
            shift_lead_access=i in (2, 3),
        )
        EmployeePayRole.objects.create(organization=org, employee=emp, pay_role=pay_role)
        held = set()
        if i == 1:
            held.add("Opener")
        if i in (2, 3) or (regular and rng.random() < 0.15):
            held.add("Shift lead")
        if 5 <= i <= 8:
            held.add("New employee")
        if regular and rng.random() < 0.2:
            held.add("Cataloguer")
        if regular and rng.random() < 0.12:
            held.add("Processor")
        for name in sorted(held):
            EmployeeTag.objects.create(organization=org, employee=emp, tag=tags[name])

    # The first student can sign in, to try the employee view.
    student = Employee.objects.get(organization=org, email="student01@example.edu")
    user = User.objects.filter(email=student.email).first() or User.objects.create_user(
        student.email, DEMO_PASSWORD
    )
    Membership.objects.create(user=user, organization=org, role=Role.EMPLOYEE)
    student.user = user
    student.save(update_fields=["user"])

    # Availability for both terms: one person hasn't entered it (scenario 8), and two have
    # imported availability waiting for review.
    for term in (fall, spring):
        for i, emp in enumerate(Employee.objects.filter(organization=org).order_by("email"), start=1):
            if i == 9:
                continue
            blocks = synthetic.opener_pattern() if i == 1 else synthetic.student_pattern(rng)
            status = Status.NEEDS_REVIEW if i in (12, 13) else Status.ENTERED
            availability = Availability.objects.create(
                organization=org, employee=emp, term=term, status=status
            )
            AvailabilityBlock.objects.bulk_create(
                AvailabilityBlock(
                    organization=org,
                    availability=availability,
                    weekday=b.weekday,
                    kind=str(b.kind),
                    start_min=b.interval.start,
                    end_min=b.interval.end,
                )
                for b in normalise_blocks(blocks)
            )

    seed_rules_and_period(org, loc, tags, spring)
    return org


def seed_rules_and_period(org, loc, tags, spring) -> None:
    """The pilot's rules (SPEC §6) and a spring period with a few draft shifts."""
    SchedulingRules.for_org(org)
    HourCapRule.objects.create(
        organization=org,
        scope="period",
        minutes=720,
        tag=tags["New employee"],
        label="New employees' period cap",
    )
    tenth = Employee.objects.filter(organization=org).order_by("email")[9]
    HourCapRule.objects.create(
        organization=org, scope="week", minutes=600, employee=tenth, label="Other campus job"
    )
    TagCoverageRule.objects.create(organization=org, tag=tags["Shift lead"], kind="min_on_duty")
    TagCoverageRule.objects.create(
        organization=org,
        tag=tags["Cataloguer"],
        kind="min_on_duty",
        weekdays=[0, 1, 2, 3, 4],
        start_min=540,
        end_min=1020,
    )
    TagCoverageRule.objects.create(
        organization=org, tag=tags["Processor"], kind="min_weekly_hours", minutes=900
    )
    TagCoverageRule.objects.create(organization=org, tag=tags["New employee"], kind="not_alone")
    period = SchedulePeriod.objects.create(
        organization=org, location=loc, start_date=date(2027, 1, 16), weeks=2
    )
    opener = Employee.objects.get(organization=org, email="student01@example.edu")
    DutyAssignment.objects.create(
        organization=org,
        employee=opener,
        kind=DutyKind.OPENER,
        weekdays=[MON, TUE, WED, THU, FRI],
        short_weekdays=[MON, WED, FRI],
        short_minutes=90,
    )
    for email, week in (("student02@example.edu", 1), ("student03@example.edu", 2)):
        lead = Employee.objects.get(organization=org, email=email)
        DutyAssignment.objects.create(
            organization=org,
            employee=lead,
            kind=DutyKind.WEEK_HOURS,
            weeks=[week],
            minutes=1200,
            off_week_max_minutes=600,
            label="lead week",
        )
    for offset in (2, 3, 4, 5, 6):  # Monday to Friday of week 1
        day = period.start_date + timedelta(days=offset)
        short = day.weekday() in (MON, WED, FRI)  # 9am class: a 1.5-hour opening shift
        Shift.objects.create(
            organization=org,
            location=loc,
            period=period,
            employee=opener,
            date=day,
            start_min=450,
            end_min=540 if short else 570,
            pay_role=opener.default_pay_role,
        )


def generate_and_publish(org: Organization, seconds: int) -> str:
    """Development helper: solve the first period from scratch and publish it, so the student
    views, exchanges and reports have something to show."""
    from app.scheduling import services
    from domain.engine import Mode, SolveRequest, run

    period = SchedulePeriod.objects.filter(organization=org).order_by("start_date").first()
    supervisor = User.objects.get(email="supervisor@example.edu")
    ctx = services.build_context(period)
    option = run(
        ctx, SolveRequest(Mode.GENERATE, time_limit=seconds, workers=settings.STACKS_SOLVER_WORKERS)
    ).options[0]
    period.shifts.all().delete()  # a fresh demo period: no history to keep
    people = {e.pk: e for e in Employee.objects.filter(organization=org)}
    Shift.objects.bulk_create(
        Shift(
            organization=org,
            location=period.location,
            period=period,
            employee=people[s.employee_id],
            date=s.day,
            start_min=s.interval.start,
            end_min=s.interval.end,
            pay_role=people[s.employee_id].default_pay_role,
        )
        for s in option.shifts
        if s.employee_id is not None
    )
    services.publish(period, supervisor)
    return option.report.summary()
