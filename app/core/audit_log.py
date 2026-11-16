# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read side of the audit log: events for one organization, described for people.

The log reads the page, not the history (D128). Who may see an entry, the filters, the order
and the count use plain columns of each event table. Only the entries on the page are then
read in full through pghistory's `Events`, which adds the before-and-after diff. Asking
`Events` for the whole log works out that diff for every entry ever recorded before it can
filter, sort or count, so the page slowed down as the history grew.
"""

from __future__ import annotations

import contextlib
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from django.apps import apps
from django.db.models import Q, QuerySet, TextField, Value
from django.db.models.functions import Cast
from pghistory.models import Event, Events

from app.accounts.models import User

HIDDEN_FIELDS = {"id", "organization_id", "created_at"}
ACTIONS = {"insert": "Added", "update": "Changed", "delete": "Removed"}


# Organization-wide settings: every supervisor sees their history (D107).
ORGANIZATION_WIDE = {
    "core.Organization",
    "core.Location",
    "core.OperatingHoursSet",
    "core.OpenInterval",
    "core.Closure",
    "core.Term",
    "core.Department",
    "core.Membership",
    "core.SupervisorDepartment",
    "core.RetentionPolicy",
    "people.PayRole",
    "people.Tag",
    "imports.ImportBatch",  # imports stay organization-wide for now (open item 17)
    "scheduling.SchedulingRules",
}
# Records that belong to a person, keyed by the employee they belong to.
BY_EMPLOYEE = {
    "people.EmployeePayRole",
    "people.EmployeeDepartment",
    "people.EmployeeTag",
    "availability.Availability",
    "availability.TimeOff",
    "scheduling.RotationAssignment",
    "scheduling.DutyAssignment",
    "scheduling.HourTargetRule",
    "scheduling.DesiredHours",
    "scheduling.CalendarFeed",
}
BY_PERIOD = {"scheduling.Shift", "scheduling.PublishedVersion", "scheduling.SolveRun"}
BY_DEPARTMENT = {"scheduling.CoverageOverride", "scheduling.TagCoverageRule"}  # blank: every department
BY_USER = {"scheduling.EmailPreference", "scheduling.PushPreference"}  # a person's notification settings

# Newest first. Entries in different tables can share a time and an id; the event table's
# name settles those, so a row never shows on two pages.
ORDER = ("-pgh_created_at", "-pgh_id", "pgh_model")

# One way for an entry to match: (column of the event table, lookup, value). A column the
# event table doesn't have never matches, as a missing key in `Events.pgh_data` didn't.
Match = tuple[str, str, Any]
EVERYTHING: tuple[Match, ...] = ()


def event_models() -> list[type[Event]]:
    """Every event table, as `Events` gathers them when it isn't told which."""
    return [
        m
        for m in apps.get_models()
        if issubclass(m, Event) and not m._meta.abstract and not m._meta.proxy and m._meta.managed
    ]


def _tracked(model: type[Event]) -> str:
    """The model an event table tracks, as `Events.pgh_obj_model` names it."""
    return model.pgh_tracked_model._meta.label


def _any(model: type[Event], matches: Iterable[Match]) -> Q | None:
    """Rows of `model` meeting any of `matches`, or None when none can apply to it."""
    columns = {f.column: f.attname for f in model._meta.concrete_fields}
    q = None
    for column, lookup, value in matches:
        if column in columns:
            part = Q(**{f"{columns[column]}__{lookup}": value})
            q = part if q is None else q | part
    return q


def events_for(org, access=None) -> AuditLog:
    """The organization's audit entries, newest first. A supervisor limited to some departments
    sees the history of organization-wide settings and of records in their departments only
    (D107); admins see everything. A model not listed here is hidden from them."""
    rules = None
    if access is not None and access.department_ids is not None:
        rules = _visible(org, access.department_ids)
    tables = []
    for model in event_models():
        label = _tracked(model)
        mine = [("organization_id", "exact", org.pk)]
        if label == "core.Organization":
            mine.append(("pgh_obj_id", "exact", org.pk))
        q = _any(model, mine)
        if q is None:
            continue  # no organization column (a supervisor's department link): never listed
        rows = model.objects.filter(q)
        if rules is not None:
            if label not in rules:
                continue
            if rules[label] is not EVERYTHING:
                if (seen := _any(model, rules[label])) is None:
                    continue
                rows = rows.filter(seen)
        tables.append(_Table(model, rows))
    return AuditLog(tables)


def _visible(org, department_ids) -> dict[str, tuple[Match, ...]]:
    """What a supervisor limited to `department_ids` sees of each tracked model (D107), checked
    against the columns of its event table. A model missing here is hidden from them."""
    from app.availability.models import Availability
    from app.people.models import Employee
    from app.scheduling.models import SchedulePeriod, Shift

    depts = list(department_ids)
    employees = Employee.objects.filter(organization=org, departments__in=depts).values("pk")
    users = Employee.objects.filter(pk__in=employees, user__isnull=False).values("user_id")
    periods = SchedulePeriod.objects.filter(organization=org, department__in=depts).values("pk")
    shifts = Shift.objects.filter(period__in=periods).values("pk")
    availabilities = Availability.objects.filter(employee__in=employees).values("pk")

    rules: dict[str, tuple[Match, ...]] = {label: EVERYTHING for label in ORGANIZATION_WIDE}
    rules["people.Employee"] = (("pgh_obj_id", "in", employees),)
    rules |= {label: (("employee_id", "in", employees),) for label in BY_EMPLOYEE}
    rules["scheduling.HourCapRule"] = (("employee_id", "isnull", True), ("employee_id", "in", employees))
    rules["availability.AvailabilityBlock"] = (("availability_id", "in", availabilities),)
    rules["scheduling.SchedulePeriod"] = (("department_id", "in", depts),)
    rules |= {label: (("period_id", "in", periods),) for label in BY_PERIOD}
    rules["scheduling.CallOut"] = (("shift_id", "in", shifts),)
    rules["scheduling.ShiftExchange"] = (("shift_id", "in", shifts), ("other_shift_id", "in", shifts))
    rules |= {
        label: (("department_id", "isnull", True), ("department_id", "in", depts)) for label in BY_DEPARTMENT
    }
    rules |= {label: (("user_id", "in", users),) for label in BY_USER}
    return rules


@dataclass(frozen=True)
class _Table:
    """One event table and the entries in it that a reader may see."""

    model: type[Event]
    rows: QuerySet

    def labelled(self) -> QuerySet:
        """The rows with the names `Events` gives the event table and the tracked model."""
        return self.rows.annotate(
            pgh_model=Value(self.model._meta.label), pgh_obj_model=Value(_tracked(self.model))
        )


class AuditLog:
    """The audit entries a reader may see, newest first.

    Enough of a queryset for `Paginator`: `count()` and slicing. A slice chooses its entries by
    time, id and table, then reads only those in full, as `Events` (D128)."""

    def __init__(self, tables: Iterable[_Table]):
        self.tables = tuple(tables)

    def filter(self, *args: Q, **kwargs: Any) -> AuditLog:
        """Narrow every table the same way, by event table columns."""
        return AuditLog(_Table(t.model, t.rows.filter(*args, **kwargs)) for t in self.tables)

    def of_model(self, label: str) -> AuditLog:
        """Only the changes to one tracked model, such as "scheduling.Shift"."""
        return AuditLog(t for t in self.tables if _tracked(t.model) == label)

    def kinds(self) -> set[str]:
        """The tracked models with at least one entry here, in one query."""
        parts = [t.labelled().values_list("pgh_obj_model", flat=True)[:1] for t in self.tables]
        return set(_union(parts)) if parts else set()

    def count(self) -> int:
        parts = [t.rows.values_list("pgh_id") for t in self.tables]
        return _union(parts).count() if parts else 0

    def keys(self, start: int = 0, stop: int | None = None) -> list[tuple[str, int, str]]:
        """(event table, id, tracked object id) for the entries from `start` to `stop`."""
        if not self.tables or (stop is not None and stop <= start):
            return []
        parts = [
            t.labelled()
            .annotate(pgh_obj_key=Cast("pgh_obj_id", TextField()))
            .values_list("pgh_created_at", "pgh_id", "pgh_model", "pgh_obj_key")
            for t in self.tables
        ]
        rows = _union(parts).order_by(*ORDER)[start:stop]
        return [(table, pgh_id, obj) for _, pgh_id, table, obj in rows]

    def __getitem__(self, k: slice) -> list[Events]:
        if not isinstance(k, slice) or k.step is not None:
            raise TypeError("The audit log can only be sliced, as audit_log[start:stop].")
        return read(self.keys(k.start or 0, k.stop))


def _union(parts: list[QuerySet]) -> QuerySet:
    return parts[0].union(*parts[1:], all=True)


def read(keys: list[tuple[str, int, str]]) -> list[Events]:
    """The entries named by `keys`, in that order, as `Events` with their diffs. One query per
    event table, limited to the objects and ids asked for, so the work depends on the page and
    not on the size of the history (D128)."""
    wanted: dict[str, dict[int, str]] = defaultdict(dict)
    for table, pgh_id, obj in keys:
        wanted[table][pgh_id] = obj
    found: dict[tuple[str, int], Events] = {}
    for table, ids in wanted.items():
        model = apps.get_model(table)
        # `tracks()` puts the records inside pghistory's query, so it reads no more than their
        # own history even if PostgreSQL didn't apply the id filter there first (it does). The
        # instances only carry a primary key: the record itself may have been deleted since.
        objs = [model.pgh_tracked_model(pk=pk) for pk in set(ids.values())]
        for event in Events.objects.across(model).tracks(objs).filter(pgh_id__in=list(ids)):
            found[(table, event.pgh_id)] = event
    # An entry purged in between (retention, D109) is left out rather than failing the page.
    return [found[(table, pgh_id)] for table, pgh_id, _ in keys if (table, pgh_id) in found]


@dataclass
class AuditRow:
    when: datetime
    who: str
    action: str
    what: str
    object_id: str
    changes: list[tuple[str, Any, Any]]
    summary: str


def _verbose(model_label: str) -> str:
    try:
        return str(apps.get_model(model_label)._meta.verbose_name).capitalize()
    except LookupError:
        return model_label


def _summary(data: dict) -> str:
    for key in ("name", "preferred_name", "reason", "role"):
        if data.get(key):
            return str(data[key])
    return ""


def filtered(rows: AuditLog, params) -> AuditLog:
    """Narrow the log by what changed, who changed it and when (audit 2.24)."""
    if what := params.get("what"):
        rows = rows.of_model(what)
    # The context is pghistory's `Context` row; `Events.pgh_context` is its metadata.
    if (who := params.get("who", "")).isdigit():
        rows = rows.filter(pgh_context__metadata__user=int(who))
    elif who == "system":
        rows = rows.filter(
            Q(pgh_context__metadata__isnull=True) | Q(pgh_context__metadata__user__isnull=True)
        )
    for key, lookup in (("from", "pgh_created_at__date__gte"), ("to", "pgh_created_at__date__lte")):
        with contextlib.suppress(ValueError):  # a missing or malformed date doesn't filter
            rows = rows.filter(**{lookup: date.fromisoformat(params.get(key, ""))})
    return rows


def _people(events) -> dict[str, dict[int, str]]:
    """Names for the employee and availability ids in a page of events, so a row can say whose
    record changed instead of a database id."""
    from app.availability.models import Availability
    from app.people.models import Employee

    employee_ids = {d.get("employee_id") for e in events if (d := e.pgh_data or {}).get("employee_id")}
    availability_ids = {
        d.get("availability_id") for e in events if (d := e.pgh_data or {}).get("availability_id")
    }
    return {
        "employee": dict(Employee.objects.filter(pk__in=employee_ids).values_list("pk", "preferred_name")),
        "availability": dict(
            Availability.objects.filter(pk__in=availability_ids).values_list("pk", "employee__preferred_name")
        ),
    }


def describe(events) -> list[AuditRow]:
    events = list(events)
    user_ids = {e.pgh_context.get("user") for e in events if e.pgh_context and e.pgh_context.get("user")}
    emails = dict(User.objects.filter(pk__in=user_ids).values_list("pk", "email"))
    people = _people(events)
    rows = []
    for e in events:
        ctx = e.pgh_context or {}
        who = emails.get(ctx.get("user"), "System" if not ctx.get("user") else "Deleted user")
        changes = [
            (field, before, after)
            for field, (before, after) in sorted((e.pgh_diff or {}).items())
            if field not in HIDDEN_FIELDS
        ]
        rows.append(
            AuditRow(
                when=e.pgh_created_at,
                who=who,
                action=ACTIONS.get(e.pgh_label, e.pgh_label),
                what=_verbose(e.pgh_obj_model),
                object_id=str(e.pgh_obj_id),
                changes=changes,
                summary=_summary(e.pgh_data or {}) or _whose(e.pgh_data or {}, people),
            )
        )
    return rows


def _whose(data: dict, people: dict[str, dict[int, str]]) -> str:
    if name := people["employee"].get(data.get("employee_id")):
        return f"for {name}"
    if name := people["availability"].get(data.get("availability_id")):
        return f"for {name}"
    return ""
