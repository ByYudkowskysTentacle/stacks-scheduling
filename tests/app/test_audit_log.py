# SPDX-License-Identifier: AGPL-3.0-or-later
"""The audit log reads the page, not the history (D128).

The read side used to be one pghistory `Events` query over every event ever recorded. It now
chooses the page from plain columns and reads only those entries through `Events`. These tests
hold it to what the old query gave: the same entries for each reader (D107) and each filter,
in the same order, described the same way. The old query is kept below as the reference.
All data is synthetic."""

from __future__ import annotations

import contextlib
import json
import random
from datetime import date, datetime, timedelta
from decimal import Decimal

import pghistory
import pytest
from django.apps import apps
from django.db import connection
from django.db.models import Q
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from pghistory.models import Events

from app.availability.models import Availability, AvailabilityBlock, TimeOff
from app.core import audit_log
from app.core.models import Department, Location, Role, Term
from app.core.permissions import Access
from app.people.models import Employee, EmployeeDepartment, EmployeePayRole, EmployeeTag, PayRole, Tag
from app.scheduling.models import (
    CallOut,
    CoverageOverride,
    DesiredHours,
    EmailPreference,
    ExchangeKind,
    ExchangeStatus,
    HourCapRule,
    PushPreference,
    SchedulePeriod,
    Shift,
    ShiftExchange,
    TagCoverageRule,
)
from tests.conftest import make_user, manages

pytestmark = pytest.mark.django_db
START = date(2027, 1, 16)
MONDAY = date(2027, 1, 18)
BASE = timezone.make_aware(datetime(2026, 9, 1, 8, 0))


# --- The read side before D128, kept as the reference ---------------------------------------


def old_events_for(org, access=None):
    rows = Events.objects.filter(
        Q(pgh_data__organization_id=org.pk) | Q(pgh_obj_model="core.Organization", pgh_obj_id=str(org.pk))
    )
    if access is not None and access.department_ids is not None:
        rows = rows.filter(old_visible(org, access.department_ids))
    return rows.order_by("-pgh_created_at", "-pgh_id")


def old_visible(org, department_ids) -> Q:
    depts = list(department_ids)
    employees = list(
        Employee.objects.filter(organization=org, departments__in=depts)
        .values_list("pk", flat=True)
        .distinct()
    )
    users = list(
        Employee.objects.filter(pk__in=employees, user__isnull=False).values_list("user_id", flat=True)
    )
    periods = list(
        SchedulePeriod.objects.filter(organization=org, department__in=depts).values_list("pk", flat=True)
    )
    shifts = list(Shift.objects.filter(period__in=periods).values_list("pk", flat=True))
    availabilities = list(Availability.objects.filter(employee__in=employees).values_list("pk", flat=True))

    def model(*labels: str) -> Q:
        return Q(pgh_obj_model__in=labels)

    return (
        model(*audit_log.ORGANIZATION_WIDE)
        | model("people.Employee") & Q(pgh_obj_id__in=[str(e) for e in employees])
        | model(*audit_log.BY_EMPLOYEE) & Q(pgh_data__employee_id__in=employees)
        | model("scheduling.HourCapRule")
        & (Q(pgh_data__employee_id=None) | Q(pgh_data__employee_id__in=employees))
        | model("availability.AvailabilityBlock") & Q(pgh_data__availability_id__in=availabilities)
        | model("scheduling.SchedulePeriod") & Q(pgh_data__department_id__in=depts)
        | model(*audit_log.BY_PERIOD) & Q(pgh_data__period_id__in=periods)
        | model("scheduling.CallOut") & Q(pgh_data__shift_id__in=shifts)
        | model("scheduling.ShiftExchange")
        & (Q(pgh_data__shift_id__in=shifts) | Q(pgh_data__other_shift_id__in=shifts))
        | model(*audit_log.BY_DEPARTMENT)
        & (Q(pgh_data__department_id=None) | Q(pgh_data__department_id__in=depts))
        | model(*audit_log.BY_USER) & Q(pgh_data__user_id__in=users)
    )


def old_filtered(rows, params):
    if what := params.get("what"):
        rows = rows.filter(pgh_obj_model=what)
    if (who := params.get("who", "")).isdigit():
        rows = rows.filter(pgh_context__user=int(who))
    elif who == "system":
        rows = rows.filter(Q(pgh_context__isnull=True) | Q(pgh_context__user__isnull=True))
    for key, lookup in (("from", "pgh_created_at__date__gte"), ("to", "pgh_created_at__date__lte")):
        with contextlib.suppress(ValueError):
            rows = rows.filter(**{lookup: date.fromisoformat(params.get(key, ""))})
    return rows


def ids(events) -> list[tuple[str, int]]:
    return [(e.pgh_model, e.pgh_id) for e in events]


# --- Mixed data: two departments, a person in both, one in neither, another organization ----


def restamp(seed: int = 7) -> None:
    """Give every audit entry its own time, in an order unrelated to tables and ids: pairs a
    microsecond apart, the pairs seven hours apart, so entries span several days. Within one
    test transaction the database would otherwise stamp them all with the same time."""
    entries = [(m, pk) for m in audit_log.event_models() for pk in m.objects.values_list("pgh_id", flat=True)]
    random.Random(seed).shuffle(entries)  # noqa: S311
    for i, (model, pk) in enumerate(entries):
        when = BASE + timedelta(hours=7 * (i // 2), microseconds=i % 2)
        model.objects.filter(pgh_id=pk).update(pgh_created_at=when)


@pytest.fixture
def mixed(org, other_org, location, pay_role, admin_user, supervisor):
    """Circulation and Special Collections, with records of every kind the D107 rules sort,
    changed by a person, by the system and by a context that names no one."""
    term = Term.objects.create(
        organization=org, name="Spring 2027", start_date=date(2027, 1, 11), end_date=date(2027, 5, 7)
    )
    circ = Department.objects.create(organization=org, name="Circulation")
    special = Department.objects.create(organization=org, name="Special Collections")
    manages(supervisor, circ)  # a department link has no organization column: never listed
    tag = Tag.objects.create(organization=org, name="Opener")

    def person(name, *depts, login=False):
        e = Employee.objects.create(
            organization=org, full_name=f"{name} Sample", preferred_name=name, default_pay_role=pay_role
        )
        for d in depts:
            EmployeeDepartment.objects.create(organization=org, employee=e, department=d)
        if login:
            e.user = make_user(org, f"{name.lower()}@example.edu", Role.EMPLOYEE)
            e.save()
        return e

    avery = person("Avery", circ, login=True)
    blake = person("Blake", special, login=True)
    casey = person("Casey", circ, special)
    drew = person("Drew")
    for e in (avery, blake):
        EmployeeTag.objects.create(organization=org, employee=e, tag=tag)
        EmployeePayRole.objects.create(organization=org, employee=e, pay_role=pay_role, wage=Decimal("14.25"))
        a = Availability.objects.create(organization=org, employee=e, term=term)
        AvailabilityBlock.objects.create(
            organization=org, availability=a, weekday=0, kind="unavailable", start_min=540, end_min=600
        )
        TimeOff.objects.create(organization=org, employee=e, start_date=MONDAY, end_date=MONDAY)
        EmailPreference.objects.create(organization=org, user=e.user, category="time_off", email=False)
    PushPreference.objects.create(organization=org, user=blake.user, category="reminder", push=False)
    DesiredHours.objects.create(organization=org, employee=casey, term=term, minutes=600)
    DesiredHours.objects.create(organization=org, employee=drew, term=term, minutes=300)
    HourCapRule.objects.create(organization=org, scope="week", minutes=1200)  # everyone's
    HourCapRule.objects.create(organization=org, employee=blake, scope="week", minutes=600)
    HourCapRule.objects.create(organization=org, employee=avery, scope="week", minutes=900)
    for dept, count in ((None, 3), (circ, 4), (special, 5)):
        CoverageOverride.objects.create(
            organization=org,
            location=location,
            department=dept,
            weekdays=[0],
            start_min=600,
            end_min=720,
            count=count,
        )
    for dept in (None, special):
        TagCoverageRule.objects.create(
            organization=org, tag=tag, kind="min_on_duty", count=1, department=dept
        )

    periods = {
        d.pk: SchedulePeriod.objects.create(
            organization=org, location=location, department=d, start_date=START, weeks=2
        )
        for d in (circ, special)
    }

    def shift(dept, who, start=540):
        return Shift.objects.create(
            organization=org,
            location=location,
            period=periods[dept.pk],
            employee=who,
            date=MONDAY,
            start_min=start,
            end_min=start + 180,
            pay_role=pay_role,
        )

    circ_shift, special_shift, casey_shift = (
        shift(circ, avery),
        shift(special, blake),
        shift(special, casey, 780),
    )
    CallOut.objects.create(organization=org, shift=circ_shift, called_out=avery, covered_by=casey)
    CallOut.objects.create(organization=org, shift=special_shift, called_out=blake)
    exchange = dict(organization=org, status=ExchangeStatus.ASKED, requester=avery)
    ShiftExchange.objects.create(
        **exchange, kind=ExchangeKind.SWAP, shift=special_shift, other_shift=circ_shift, taker=blake
    )
    ShiftExchange.objects.create(**exchange, kind=ExchangeKind.COVER, shift=casey_shift)

    # Changes by a person (as the request middleware records them), by the system and by a
    # context that names no one, plus a removal.
    with pghistory.context(user=admin_user.pk, url="/settings/"):
        circ.name = "Circulation Desk"
        circ.save()
        blake.default_wage = Decimal("15.75")
        blake.save()
        org.name = "Test Library (renamed)"
        org.save()
    with pghistory.context(user=supervisor.pk, url="/schedule/"):
        circ_shift.start_min = 600
        circ_shift.note = "Desk"
        circ_shift.save()
        special_shift.employee = casey
        special_shift.save()
    with pghistory.context(action="publish", period=periods[special.pk].pk):
        periods[special.pk].weeks = 3
        periods[special.pk].save()
    with pghistory.context(user=None):
        avery.default_wage = Decimal("14.50")
        avery.save()
    extra = shift(circ, drew, 960)
    extra.delete()

    # Another organization's history must never show.
    other_loc = Location.objects.create(organization=other_org, name="Elsewhere")
    other_role = PayRole.objects.create(organization=other_org, name="Assistant")
    other_period = SchedulePeriod.objects.create(organization=other_org, location=other_loc, start_date=START)
    other_person = Employee.objects.create(
        organization=other_org, full_name="Other Person", preferred_name="O"
    )
    Shift.objects.create(
        organization=other_org,
        location=other_loc,
        period=other_period,
        employee=other_person,
        date=MONDAY,
        start_min=540,
        end_min=720,
        pay_role=other_role,
    )
    other_org.name = "Other Library (renamed)"
    other_org.save()
    restamp()
    readers = {
        "admin": Access(department_ids=None),
        "circulation": Access(department_ids=frozenset({circ.pk})),
        "special": Access(department_ids=frozenset({special.pk})),
        "both": Access(department_ids=frozenset({circ.pk, special.pk})),
        "no departments": Access(department_ids=frozenset()),
    }
    return {"readers": readers, "admin": admin_user, "supervisor": supervisor, "periods": periods}


def same_as_before(org, access, params):
    old = list(old_filtered(old_events_for(org, access), params))
    log = audit_log.filtered(audit_log.events_for(org, access), params)
    new = log[0:]
    assert ids(new) == ids(old), params
    assert log.count() == len(old), params
    assert audit_log.describe(new) == audit_log.describe(old), params
    return new


# --- Who sees what (D107) -------------------------------------------------------------------


def test_every_reader_sees_exactly_what_they_saw_before(org, mixed):
    seen = {}
    for name, access in mixed["readers"].items():
        new = same_as_before(org, access, {})
        seen[name] = set(ids(new))
        everything = audit_log.events_for(org, access)
        assert everything.kinds() == set(old_events_for(org, access).values_list("pgh_obj_model", flat=True))
    assert seen["admin"] == set(ids(audit_log.events_for(org)[0:]))  # no access: everything
    # The data reaches every kind of rule, and the rules really narrow it.
    assert {
        "core.Organization",
        "core.Department",
        "core.Membership",
        "people.Employee",
        "people.EmployeePayRole",
        "people.EmployeeDepartment",
        "people.EmployeeTag",
        "availability.Availability",
        "availability.AvailabilityBlock",
        "availability.TimeOff",
        "scheduling.DesiredHours",
        "scheduling.HourCapRule",
        "scheduling.SchedulePeriod",
        "scheduling.Shift",
        "scheduling.CallOut",
        "scheduling.ShiftExchange",
        "scheduling.CoverageOverride",
        "scheduling.TagCoverageRule",
        "scheduling.EmailPreference",
        "scheduling.PushPreference",
    } <= audit_log.events_for(org).kinds()
    assert seen["no departments"] < seen["circulation"] < seen["both"] < seen["admin"]
    assert seen["special"] < seen["both"] and not seen["circulation"] <= seen["special"]
    assert not any(t == "core.SupervisorDepartmentEvent" for t, _ in seen["admin"])  # no organization


def test_another_organizations_history_never_shows(org, other_org, mixed):
    mine = audit_log.describe(audit_log.events_for(org)[0:])
    assert mine and not any("Other" in (r.summary or "") for r in mine)
    theirs = audit_log.describe(audit_log.events_for(other_org)[0:])
    assert {r.summary for r in theirs} >= {"Other Library (renamed)", "O"}
    assert not any(r.summary in ("Avery", "Blake", "Casey", "Drew") for r in theirs)


# --- Filters (audit 2.24) -------------------------------------------------------------------


def test_each_filter_matches_the_old_query_for_every_reader(org, mixed):
    admin, supervisor = mixed["admin"], mixed["supervisor"]
    days = sorted({timezone.localdate(e.pgh_created_at) for e in old_events_for(org)})
    first, middle = days[0], days[len(days) // 2]
    assert len(days) > 3
    whats = [{"what": label} for label in [*sorted(audit_log.events_for(org).kinds()), "scheduling.Nothing"]]
    others = [
        {"who": str(admin.pk)},
        {"who": str(supervisor.pk)},
        {"who": "system"},
        {"who": "999999"},
        {"who": "someone"},
        {"from": middle.isoformat()},
        {"to": middle.isoformat()},
        {"from": first.isoformat(), "to": middle.isoformat()},
        {"from": "2999-01-01"},
        {"from": "not a date", "to": ""},
        {"what": "scheduling.Shift", "who": "system", "from": first.isoformat()},
        {"what": "people.Employee", "who": str(admin.pk)},
    ]
    for name, access in mixed["readers"].items():
        # Every model on its own for an admin and a one-department supervisor; the rest for all.
        for p in (whats if name in ("admin", "circulation") else []) + others:
            same_as_before(org, access, p)


def test_filters_narrow_as_labelled(as_user, org, mixed):
    admin, supervisor = mixed["admin"], mixed["supervisor"]
    log = audit_log.events_for(org)
    by_admin = audit_log.describe(audit_log.filtered(log, {"who": str(admin.pk)})[0:])
    assert by_admin and {r.who for r in by_admin} == {"admin@example.edu"}
    system = audit_log.describe(audit_log.filtered(log, {"who": "system"})[0:])
    assert system and {r.who for r in system} == {"System"}
    shifts = audit_log.describe(audit_log.filtered(log, {"what": "scheduling.Shift"})[0:])
    assert shifts and {r.what for r in shifts} == {"Shift"}
    assert {r.action for r in shifts} == {"Added", "Changed", "Removed"}
    assert audit_log.filtered(log, {"from": "2999-01-01"}).count() == 0
    page = as_user(supervisor).get(reverse("audit_log") + f"?who={supervisor.pk}").content.decode()
    assert "supervisor@example.edu" in page and "admin@example.edu</td>" not in page


# --- Diffs ----------------------------------------------------------------------------------


def test_a_diff_compares_each_entry_with_the_previous_one_for_the_same_record(org, location, pay_role):
    people = [
        Employee.objects.create(organization=org, full_name=f"P{i} Sample", preferred_name=f"P{i}")
        for i in range(2)
    ]
    period = SchedulePeriod.objects.create(organization=org, location=location, start_date=START)
    shift = Shift.objects.create(
        organization=org,
        location=location,
        period=period,
        employee=people[0],
        date=MONDAY,
        start_min=540,
        end_min=720,
        pay_role=pay_role,
    )
    other = Shift.objects.create(
        organization=org,
        location=location,
        period=period,
        employee=people[1],
        date=MONDAY,
        start_min=780,
        end_min=900,
        pay_role=pay_role,
    )
    other.note = "Someone else's shift, edited in between"
    other.save()
    shift.start_min, shift.note = 600, "Desk"
    shift.save()
    shift.employee = people[1]
    shift.save()
    people[0].default_wage = Decimal("13.00")
    people[0].save()
    shift_id = str(shift.pk)
    shift.delete()
    restamp()
    events = audit_log.events_for(org)[0:]
    assert audit_log.describe(events) == audit_log.describe(old_events_for(org))

    rows = [r for r in audit_log.describe(events) if r.what == "Shift" and r.object_id == shift_id]
    by_action = {}
    for r in rows:
        by_action.setdefault(r.action, []).append(r)
    added, changed, removed = by_action["Added"], by_action["Changed"], by_action["Removed"]
    assert len(added) == 1 and added[0].changes == []  # an insert has no "before"
    changes = sorted(({f: (b, a) for f, b, a in c.changes} for c in changed), key=len, reverse=True)
    assert changes[0]["start_min"] == (540, 600) and changes[0]["note"] == ("", "Desk")
    assert changes[1]["employee_id"] == (people[0].pk, people[1].pk) and "start_min" not in changes[1]
    assert all("updated_at" in c for c in changes)  # each against its own previous entry
    assert len(removed) == 1 and removed[0].changes == []  # the same as the entry before it
    wage = next(r for r in audit_log.describe(events) if r.what == "Employee" and r.action == "Changed")
    assert dict((f, (b, a)) for f, b, a in wage.changes)["default_wage"] == (None, 13.0)


# --- Order and pages ------------------------------------------------------------------------


def test_order_is_newest_first_across_tables_with_close_and_equal_times(org):
    tag = Tag.objects.create(organization=org, name="Opener")
    term = Term.objects.create(
        organization=org, name="Fall", start_date=date(2026, 8, 24), end_date=date(2026, 12, 11)
    )
    tag.name = "Closer"
    tag.save()
    term.name = "Fall 2026"
    term.save()
    tag_event, term_event = apps.get_model("people", "TagEvent"), apps.get_model("core", "TermEvent")
    org_event = apps.get_model("core", "OrganizationEvent")
    for model in audit_log.event_models():  # everything else older
        model.objects.update(pgh_created_at=BASE - timedelta(days=1))
    (t1, t2), (s1, s2) = (
        list(m.objects.order_by("pgh_id").values_list("pgh_id", flat=True)) for m in (tag_event, term_event)
    )
    (o1,) = org_event.objects.filter(pgh_obj_id=org.pk).values_list("pgh_id", flat=True)
    tied = max(t2, s2, o1) + 1000  # two tables, the same time and the same id
    tag_event.objects.filter(pgh_id=t2).update(pgh_id=tied)
    term_event.objects.filter(pgh_id=s2).update(pgh_id=tied)
    just_before = BASE - timedelta(microseconds=1)
    for model, pk, when in (
        (tag_event, t1, BASE),
        (term_event, s1, BASE + timedelta(microseconds=1)),
        (tag_event, tied, just_before),
        (term_event, tied, just_before),
        (org_event, o1, just_before),
    ):
        model.objects.filter(pgh_id=pk).update(pgh_created_at=when)
    log = audit_log.events_for(org)
    assert ids(log[0:5]) == [
        ("core.TermEvent", s1),  # a microsecond newer
        ("people.TagEvent", t1),
        ("core.TermEvent", tied),  # same time and id: by the table's name
        ("people.TagEvent", tied),
        ("core.OrganizationEvent", o1),  # same time, lower id
    ]
    # One entry per page gives the same order, so a tied entry never shows on two pages.
    assert [ids(log[i : i + 1])[0] for i in range(log.count())] == ids(log[0:])
    assert sorted(ids(old_events_for(org))) == sorted(ids(log[0:]))


def test_the_second_page_is_the_next_fifty(as_user, admin_user, org, location, pay_role):
    tag = Tag.objects.create(organization=org, name="Tag 0")
    for i in range(1, 120):
        tag.name = f"Tag {i}"
        tag.save()
    restamp()
    log = audit_log.events_for(org)
    old = list(old_events_for(org))
    assert len(old) > 100
    assert audit_log.describe(log[50:100]) == audit_log.describe(old[50:100])
    page = as_user(admin_user).get(reverse("audit_log") + "?page=2").content.decode()
    assert f"Page 2 of {-(-len(old) // 50)}" in page

    def listed(row):
        return f" {row.summary}</td>" in page

    assert all(listed(r) for r in audit_log.describe(old[50:100]) if r.summary)
    assert not any(listed(r) for r in audit_log.describe(old[:50]) if r.summary)


# --- Cost: the page, not the history --------------------------------------------------------


def jsonb_each_loops(sql: str, params=None) -> int:
    """The most times any single jsonb_each ran in `sql`: once per entry pghistory builds
    `pgh_data` or `pgh_diff` for."""
    if sql.lstrip().startswith("SELECT set_config"):  # pghistory's context for the request
        sql = sql.split(";", 1)[1]
    with connection.cursor() as cur:
        cur.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + sql, params)
        (plan,) = cur.fetchone()
    plan = json.loads(plan) if isinstance(plan, str) else plan

    def walk(node):
        own = node.get("Actual Loops", 0) if node.get("Function Name") == "jsonb_each" else 0
        return max([own] + [walk(child) for child in node.get("Plans", [])])

    return walk(plan[0]["Plan"])


def test_a_page_costs_the_same_however_long_the_history(
    as_user, admin_user, org, location, pay_role, employee
):
    period = SchedulePeriod.objects.create(organization=org, location=location, start_date=START)
    shift = Shift.objects.create(
        organization=org,
        location=location,
        period=period,
        employee=employee,
        date=MONDAY,
        start_min=540,
        end_min=720,
        pay_role=pay_role,
    )
    client = as_user(admin_user)
    url = reverse("audit_log") + "?page=2"

    def page():
        with CaptureQueriesContext(connection) as q:
            assert client.get(url).status_code == 200
        return q.captured_queries

    def edit(times):
        # The same person makes every change. (Within the test's one transaction, a request's
        # context would otherwise stick to the changes made after it.)
        with pghistory.context(user=admin_user.pk):
            for i in range(times):
                shift.note = f"Edit {i}"
                shift.save()
        for model in audit_log.event_models():  # pages 1 and 2 hold shift changes only
            if model is not Shift.pgh_event_model:
                model.objects.update(pgh_created_at=BASE)

    edit(120)
    short = page()
    edit(400)
    long = page()
    assert len(long) == len(short)
    # Diffs are worked out for the entries on the page only, never for the whole history:
    # each statement that builds them runs jsonb_each at most once per entry on the page.
    builders = [q["sql"] for q in long if "jsonb_each" in q["sql"].lower()]
    assert builders and all(jsonb_each_loops(sql) <= 50 for sql in builders)
    assert jsonb_each_loops(*old_events_for(org).query.sql_with_params()) > 500  # the old query
