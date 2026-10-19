# SPDX-License-Identifier: AGPL-3.0-or-later
"""Removing a department (D110). A department with nothing in it is deleted; one that holds
schedules, people, supervisors or coverage rules is first merged into another department, so
every schedule, shift and history row keeps its ID (CLAUDE.md §2.3). Each moved row and the
deletion itself are recorded in the audit log by the row triggers."""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction

from app.core.models import Department


class DepartmentError(Exception):
    pass


@dataclass(frozen=True)
class Usage:
    periods: int
    people: int
    supervisors: int
    coverage_overrides: int
    tag_rules: int

    @property
    def empty(self) -> bool:
        return not (
            self.periods or self.people or self.supervisors or self.coverage_overrides or self.tag_rules
        )


def usage(department: Department) -> Usage:
    return Usage(
        periods=department.periods.count(),
        people=department.employee_links.count(),
        supervisors=department.supervisor_links.count(),
        coverage_overrides=department.coverage_overrides.count(),
        tag_rules=department.tag_rules.count(),
    )


def clashes(department: Department, into: Department) -> list:
    """Schedules of `department` that start the same week at the same location as one of `into`'s.
    Two schedules for one department and week can't exist, so these block the merge."""
    taken = set(into.periods.values_list("location_id", "start_date"))
    return [
        p
        for p in department.periods.select_related("location").order_by("start_date")
        if (p.location_id, p.start_date) in taken
    ]


def remove(department: Department, into: Department | None = None) -> list[str]:
    """Delete `department`, first moving what it holds into `into`. Returns the names of other
    departments that showed its open shifts to their people (D108); they no longer do."""
    with transaction.atomic():
        org = department.organization
        locked = list(Department.objects.select_for_update().filter(organization=org).order_by("pk"))
        if len(locked) == 1:
            raise DepartmentError(
                "This is the only department. Add another before removing it, or rename it instead."
            )
        found = usage(department)
        if into is None and not found.empty:
            raise DepartmentError("Choose a department to move its schedules, people and rules into.")
        if into is not None:
            if into.pk == department.pk or into.organization_id != org.pk:
                raise DepartmentError("Choose a different department to move things into.")
            clash = clashes(department, into)
            if clash:
                weeks = ", ".join(f"{p.location} from {p.start_date:%-d %b %Y}" for p in clash)
                raise DepartmentError(
                    f"{into} already has a schedule for the same week and location as: {weeks}. "
                    "Move or remove those schedules first."
                )
            department.periods.update(department=into)
            department.coverage_overrides.update(department=into)
            department.tag_rules.update(department=into)
            already = into.employee_links.values("employee_id")
            department.employee_links.filter(employee_id__in=already).delete()
            department.employee_links.update(department=into)
            already = into.supervisor_links.values("membership_id")
            department.supervisor_links.filter(membership_id__in=already).delete()
            department.supervisor_links.update(department=into)
        showed = []
        for other in locked:
            if other.pk != department.pk and department.pk in (other.open_shifts_from or []):
                other.open_shifts_from = [pk for pk in other.open_shifts_from if pk != department.pk]
                other.save(update_fields=["open_shifts_from"])
                showed.append(other.name)
        department.delete()
        return showed
