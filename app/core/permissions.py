# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one permission layer (ADR 0001, CLAUDE.md §6). Every view checks access here.

Roles (SPEC §1): employees see their own data plus the published schedule;
supervisors manage their organization; admins also manage who has which role.
Shift-lead rights (D25) are a per-employee grant on top of the employee role,
never a consequence of the `Shift lead` tag (SPEC §4.3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.models import Membership, Organization
    from app.people.models import Employee


class Perm(StrEnum):
    CONFIGURE = "org.configure"  # operating hours, closures, terms, pay roles, tags
    MANAGE_MEMBERS = "org.members"  # grant supervisor and admin roles
    VIEW_ROSTER = "people.view"  # the employee list and records
    MANAGE_PEOPLE = "people.manage"  # create, edit, archive and invite employees
    VIEW_PAY = "people.view_pay"  # wages (D48)
    VIEW_NOTES = "people.view_notes"  # supervisor notes
    VIEW_AUDIT = "audit.view"
    EDIT_SCHEDULE = "schedule.edit"  # add, change and remove draft shifts; review lead proposals
    PUBLISH = "schedule.publish"
    VIEW_DRAFTS = "schedule.view_drafts"  # the working copy, not just the published schedule
    # Shift-lead grants (D25), used from milestone 3.
    VIEW_FREE_BUSY = "availability.view_free_busy"
    VIEW_HOURS = "schedule.view_hours"
    PROPOSE_EDITS = "schedule.propose"
    RECORD_CALL_OUTS = "schedule.call_outs"


SUPERVISOR_PERMS = frozenset(Perm) - {Perm.MANAGE_MEMBERS}
ADMIN_PERMS = frozenset(Perm)
SHIFT_LEAD_PERMS = frozenset(
    {Perm.VIEW_FREE_BUSY, Perm.VIEW_HOURS, Perm.PROPOSE_EDITS, Perm.RECORD_CALL_OUTS, Perm.VIEW_DRAFTS}
)


@dataclass(frozen=True)
class Access:
    """What the signed-in user may do in the current organization."""

    organization: Organization | None = None
    membership: Membership | None = None
    employee: Employee | None = None
    perms: frozenset[Perm] = field(default_factory=frozenset)

    def has(self, perm: Perm) -> bool:
        return perm in self.perms

    @property
    def is_supervisor(self) -> bool:
        return Perm.MANAGE_PEOPLE in self.perms

    @property
    def is_shift_lead(self) -> bool:
        return Perm.PROPOSE_EDITS in self.perms and not self.is_supervisor


NO_ACCESS = Access()


def resolve_access(membership: Membership | None, employee: Employee | None) -> Access:
    from app.core.models import Role

    if membership is None:
        return NO_ACCESS
    if membership.role == Role.ADMIN:
        perms = ADMIN_PERMS
    elif membership.role == Role.SUPERVISOR:
        perms = SUPERVISOR_PERMS
    else:
        perms = frozenset()
        if employee is not None and employee.shift_lead_access and not employee.is_archived:
            perms = SHIFT_LEAD_PERMS
    return Access(membership.organization, membership, employee, frozenset(perms))
