# SPDX-License-Identifier: AGPL-3.0-or-later
"""Schedule services: build the domain context, validate, publish and discard drafts."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import pghistory
from django.conf import settings
from django.db import transaction
from django.db.models import Max, Q
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from app.availability.models import availability_for
from app.core.models import Term
from app.people.models import Employee, EmployeeTag, EmploymentType
from app.scheduling.models import (
    CoverageOverride,
    HourCapRule,
    PublishedVersion,
    SchedulePeriod,
    SchedulingRules,
    Shift,
    TagCoverageRule,
)
from domain import rules as dom
from domain.schedule import Period, Person, ScheduleContext
from domain.schedule import Shift as DomShift
from domain.timeutil import Interval
from domain.validation import Report, day_label, validate


def to_domain_shift(s: Shift) -> DomShift:
    return DomShift(s.pk, s.employee_id, s.date, s.interval, s.pay_role_id, s.location_id)


def snapshot_to_domain(row: dict[str, Any]) -> DomShift:
    return DomShift(
        row["id"],
        row["employee_id"],
        date.fromisoformat(row["date"]),
        Interval(row["start"], row["end"]),
        row.get("pay_role_id"),
        row.get("location_id"),
    )


def domain_rules(org, term: Term | None = None) -> dom.Rules:
    """The organization's rules as domain objects. Desired hours count only when approved
    for `term` (D32)."""
    from app.scheduling.models import (
        DesiredHours,
        DesiredStatus,
        DutyAssignment,
        HourTargetRule,
        RotationAssignment,
    )

    sr = SchedulingRules.for_org(org)
    caps = [c.to_domain() for c in HourCapRule.objects.filter(organization=org).select_related("tag")]
    if sr.default_weekly_cap_minutes:
        caps.insert(
            0, dom.HourCap(dom.CapScope.WEEK, sr.default_weekly_cap_minutes, label="default weekly cap")
        )
    overrides = [o.to_domain() for o in CoverageOverride.objects.filter(organization=org)]
    duties = [
        d.to_domain()
        for d in DutyAssignment.objects.filter(organization=org, employee__archived_at__isnull=True)
    ]
    return dom.Rules(
        shape=sr.shape(),
        coverage=dom.CoverageRules(sr.coverage_default, dom.CoverageMode(sr.coverage_mode), overrides),
        caps=caps,
        tag_rules=[
            r.to_domain() for r in TagCoverageRule.objects.filter(organization=org).select_related("tag")
        ],
        rotation=dom.Rotation(
            dom.RotationMode(sr.rotation_mode),
            dict(RotationAssignment.objects.filter(organization=org).values_list("employee_id", "group")),
        ),
        openers=[d for d in duties if isinstance(d, dom.OpenerDuty)],
        week_duties=[d for d in duties if isinstance(d, dom.WeekHoursDuty)],
        targets=[t.to_domain() for t in HourTargetRule.objects.filter(organization=org)],
        desired=[
            dom.DesiredHours(d.employee_id, d.minutes)  # work-study priority waits for award imports (D34)
            for d in DesiredHours.objects.filter(organization=org, term=term, status=DesiredStatus.APPROVED)
        ]
        if term is not None
        else [],
    )


def term_for(period: SchedulePeriod) -> Term | None:
    """The term whose availability applies: the one containing the period's start, else the
    one containing its end (a period that starts in a break)."""
    terms = Term.objects.filter(organization=period.organization)
    return (
        terms.filter(start_date__lte=period.start_date, end_date__gte=period.start_date).first()
        or terms.filter(start_date__lte=period.end_date, end_date__gte=period.end_date).first()
    )


def in_department(period: SchedulePeriod) -> Q:
    """Members of the period's department (D95). Every period has one after the departments
    migration; the fallback keeps a stray period without one org-wide."""
    return Q(departments=period.department_id) if period.department_id else Q()


def people_for(period: SchedulePeriod, shifts) -> dict[int, Person]:
    org = period.organization
    ids = {s.employee_id for s in shifts if s.employee_id}
    employees = list(
        Employee.objects.filter(organization=org)
        .filter(Q(Q(archived_at__isnull=True) & in_department(period)) | Q(pk__in=ids))
        .prefetch_related("employee_tags__tag")
        .distinct()
    )
    term = term_for(period)
    avail = availability_for(employees, term) if term else {}
    from app.availability.models import NOT_ENTERED, time_off_for

    off = time_off_for(employees, period.start_date, period.end_date)

    people: dict[int, Person] = {}
    for e in employees:
        tags_by_day = {d: frozenset(t.name for t in e.tags_on(d)) for d in period.days}
        people[e.pk] = Person(
            id=e.pk,
            name=e.display_name,
            availability=avail.get(e.pk, NOT_ENTERED),
            tags_by_day=tags_by_day,
            auto_schedulable=(
                e.employment_type == EmploymentType.REGULAR and e.in_scheduling_pool and not e.is_archived
            ),
            time_off=off.get(e.pk, {}),
        )
    return people


def build_context(period: SchedulePeriod, shifts=None) -> ScheduleContext:
    org = period.organization
    if shifts is None:
        shifts = list(period.shifts.live().select_related("employee"))
    term = term_for(period)
    rules = domain_rules(org, term)
    outside: dict[int, int] = {}
    if term is not None and any(c.scope == dom.CapScope.TERM for c in rules.caps):
        rows = (
            Shift.objects.live()
            .filter(
                organization=org, date__gte=term.start_date, date__lte=term.end_date, employee__isnull=False
            )
            .exclude(period=period)
        )
        for s in rows:
            outside[s.employee_id] = outside.get(s.employee_id, 0) + s.minutes
    latest = period.latest_version()
    people = people_for(period, shifts)
    # The same people's shifts on other schedules in these weeks count toward caps and double
    # booking here (D95).
    elsewhere = (
        Shift.objects.live()
        .filter(
            organization=org,
            date__gte=period.start_date,
            date__lte=period.end_date,
            employee_id__in=list(people),
        )
        .exclude(period=period)
    )
    return ScheduleContext(
        period=Period(period.start_date, period.weeks, org.week_start),
        hours=period.location.domain_hours(),
        closures=period.location.domain_closures(),
        people=people,
        rules=rules,
        shifts=[to_domain_shift(s) for s in shifts],
        published=[snapshot_to_domain(r) for r in latest.shifts] if latest else None,
        term_minutes_outside_period=outside,
        elsewhere=[to_domain_shift(s) for s in elsewhere],
    )


def validate_period(period: SchedulePeriod) -> Report:
    return validate(build_context(period))


# --- Per-shift status and publish summaries -----------------------------------------------------


def shift_states(period: SchedulePeriod) -> dict[int, str]:
    """`published`, `changed` or `new` for each live shift, against the latest version."""
    latest = period.latest_version()
    published = {r["id"]: snapshot_to_domain(r) for r in latest.shifts} if latest else {}
    out = {}
    for s in period.shifts.live():
        old = published.get(s.pk)
        if old is None:
            out[s.pk] = "new"
        elif old.same_assignment(to_domain_shift(s)):
            out[s.pk] = "published"
        else:
            out[s.pk] = "changed"
    return out


@dataclass
class PersonChanges:
    added: list[str] = field(default_factory=lambda: [])
    removed: list[str] = field(default_factory=lambda: [])

    @property
    def any(self) -> bool:
        return bool(self.added or self.removed)


def _label(s: DomShift) -> str:
    return f"{day_label(s.day)} {s.interval.label()}"


def changes_by_person(old: list[DomShift], new: list[DomShift]) -> dict[int, PersonChanges]:
    """What each employee gains and loses between two versions. A retimed or reassigned shift
    shows as removed from the old holder and added to the new one."""
    out: dict[int, PersonChanges] = defaultdict(PersonChanges)
    old_by_id = {s.id: s for s in old}
    new_by_id = {s.id: s for s in new}
    for sid, s in new_by_id.items():
        before = old_by_id.get(sid)
        if before is not None and before.same_assignment(s):
            continue
        if before is not None and before.employee_id is not None:
            out[before.employee_id].removed.append(_label(before))
        if s.employee_id is not None:
            out[s.employee_id].added.append(_label(s))
    for sid, s in old_by_id.items():
        if sid not in new_by_id and s.employee_id is not None:
            out[s.employee_id].removed.append(_label(s))
    return {k: v for k, v in out.items() if v.any}


def pending_changes(period: SchedulePeriod) -> dict[int, PersonChanges]:
    latest = period.latest_version()
    old = [snapshot_to_domain(r) for r in latest.shifts] if latest else []
    new = [to_domain_shift(s) for s in period.shifts.live()]
    return changes_by_person(old, new)


class PublishError(Exception):
    pass


def publish(period: SchedulePeriod, user, *, site_url: str = "") -> PublishedVersion:
    """Snapshot the working copy and notify exactly the affected employees, once each
    (SPEC §9.1, scenario 16). Emails go out only after the transaction commits."""
    with transaction.atomic(), pghistory.context(action="publish", period=period.pk):
        period = SchedulePeriod.objects.select_for_update().get(pk=period.pk)
        changes = pending_changes(period)
        if not changes and period.latest_version() is not None:
            raise PublishError("Nothing has changed since the last publish.")
        report = validate_period(period)
        number = (period.versions.aggregate(n=Max("number"))["n"] or 0) + 1
        live = list(period.shifts.live())
        version = PublishedVersion.objects.create(
            organization=period.organization,
            period=period,
            number=number,
            published_by=user,
            shifts=[s.snapshot() for s in live],
            validation_summary=report.summary(),
            issues=[
                {"category": i.category.value, "severity": i.severity.value, "message": i.message}
                for i in report.issues
            ],
            changes={str(k): {"added": v.added, "removed": v.removed} for k, v in changes.items()},
        )
        period.shifts.live().filter(proposed_by_lead=True).update(proposed_by_lead=False)
        _notify(period, version, changes, site_url)
    return version


def _notify(
    period: SchedulePeriod, version: PublishedVersion, changes: dict[int, PersonChanges], site_url: str
) -> None:
    link = reverse("my_schedule")
    from app.scheduling.models import NotificationCategory
    from app.scheduling.notify import notify

    employees = Employee.objects.filter(pk__in=changes.keys()).select_related("user")
    for e in employees:
        c = changes[e.pk]
        if e.user is None:
            continue  # not invited yet: nobody to tell in the app or by email
        subject = f"{settings.STACKS_SITE_NAME}: your schedule for {period} has changed"
        # D14: only the employee's own changes and a link. No wages, hour totals or co-workers.
        body = render_to_string(
            "scheduling/email/published.txt",
            {
                "employee": e,
                "period": period,
                "added": c.added,
                "removed": c.removed,
                "link": site_url + link,
            },
        )
        notify(e.user, period.organization, NotificationCategory.PUBLISHED, subject, body, link)


def discard_drafts(period: SchedulePeriod) -> int:
    """Put the working copy back to the latest published version. Shifts added since are
    soft-deleted (restorable), removed ones are restored. Returns the number of shifts touched."""
    latest = period.latest_version()
    if latest is None:
        raise PublishError("This period hasn't been published yet, so there's nothing to go back to.")
    touched = 0
    with transaction.atomic(), pghistory.context(action="discard_drafts", period=period.pk):
        published = {r["id"]: r for r in latest.shifts}
        for s in period.shifts.all():
            row = published.get(s.pk)
            if row is None:
                if s.deleted_at is None:
                    s.deleted_at = timezone.now()
                    s.save(update_fields=["deleted_at", "updated_at"])
                    touched += 1
                continue
            wanted = {
                "employee_id": row["employee_id"],
                "date": date.fromisoformat(row["date"]),
                "start_min": row["start"],
                "end_min": row["end"],
                "pay_role_id": row["pay_role_id"],
                "note": row.get("note", ""),
                "deleted_at": None,
                "proposed_by_lead": False,
            }
            if any(getattr(s, k) != v for k, v in wanted.items()):
                for k, v in wanted.items():
                    setattr(s, k, v)
                s.save()
                touched += 1
    return touched


# --- Hours report -------------------------------------------------------------------------------


@dataclass
class HoursRow:
    employee: Employee
    weeks: list[tuple[int, int | None]]  # (minutes, cap) per week
    period_minutes: int
    period_cap: int | None

    @property
    def over(self) -> bool:
        return any(cap is not None and m > cap for m, cap in self.weeks) or (
            self.period_cap is not None and self.period_minutes > self.period_cap
        )


def hours_rows(period: SchedulePeriod, employees, ctx: ScheduleContext | None = None) -> list[HoursRow]:
    """Scheduled hours against limits per person (SPEC §14), from the working copy."""
    from domain.schedule import minutes_by_person_week
    from domain.validation import effective_period_cap, effective_weekly_cap

    ctx = ctx or build_context(period)
    per_week = minutes_by_person_week(ctx.shifts, ctx.period)
    rows = []
    for e in employees:
        person = ctx.people.get(e.pk)
        if person is None:
            continue
        weeks = [
            (per_week.get(e.pk, {}).get(w, 0), effective_weekly_cap(ctx, person, w))
            for w in range(period.weeks)
        ]
        rows.append(HoursRow(e, weeks, sum(m for m, _ in weeks), effective_period_cap(ctx, person)))
    return rows


def employees_for_grid(period: SchedulePeriod):
    """Everyone in the department's scheduling pool, plus anyone else holding a shift in the period."""
    with_shifts = period.shifts.live().exclude(employee=None).values_list("employee_id", flat=True)
    return (
        Employee.objects.filter(organization=period.organization)
        .filter(
            Q(Q(archived_at__isnull=True, in_scheduling_pool=True) & in_department(period))
            | Q(pk__in=with_shifts)
        )
        .exclude(Q(employment_type=EmploymentType.INACTIVE) & ~Q(pk__in=with_shifts))
        .prefetch_related(models_prefetch_tags())
        .distinct()
    )


def models_prefetch_tags():
    from django.db.models import Prefetch

    return Prefetch("employee_tags", queryset=EmployeeTag.objects.select_related("tag"))


def revert_shift(shift: Shift, user) -> None:
    """Put one shift back to its latest published state, or remove it if it's new since then."""
    latest = shift.period.latest_version()
    row = next((r for r in latest.shifts if r["id"] == shift.pk), None) if latest else None
    if row is None:
        shift.deleted_at = timezone.now()
    else:
        shift.employee_id = row["employee_id"]
        shift.date = date.fromisoformat(row["date"])
        shift.start_min, shift.end_min = row["start"], row["end"]
        shift.pay_role_id = row["pay_role_id"]
        shift.note = row.get("note", "")
        shift.deleted_at = None
    shift.proposed_by_lead = False
    shift.updated_by = user
    shift.save()


@dataclass(frozen=True)
class PublishedShift:
    day: date
    interval: Interval
    pay_role_id: int | None
    note: str
    period: SchedulePeriod


def published_shifts_for(employee: Employee, from_day: date) -> list[PublishedShift]:
    """An employee's shifts from each period's latest published version, from `from_day` on."""
    out = []
    periods = SchedulePeriod.objects.filter(
        organization=employee.organization, start_date__gte=from_day - timedelta(days=28)
    )
    for period in periods:
        latest = period.latest_version()
        if latest is None:
            continue
        for row in latest.shifts:
            day = date.fromisoformat(row["date"])
            if row["employee_id"] == employee.pk and day >= from_day:
                out.append(
                    PublishedShift(
                        day,
                        Interval(row["start"], row["end"]),
                        row.get("pay_role_id"),
                        row.get("note", ""),
                        period,
                    )
                )
    return sorted(out, key=lambda s: (s.day, s.interval))
