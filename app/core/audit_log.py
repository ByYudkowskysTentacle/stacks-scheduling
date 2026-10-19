# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read side of the audit log: events for one organization, described for people."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.apps import apps
from django.db.models import Q, QuerySet
from pghistory.models import Events

from app.accounts.models import User

HIDDEN_FIELDS = {"id", "organization_id", "created_at"}
ACTIONS = {"insert": "Added", "update": "Changed", "delete": "Removed"}


def events_for(org) -> QuerySet:
    return Events.objects.filter(
        Q(pgh_data__organization_id=org.pk) | Q(pgh_obj_model="core.Organization", pgh_obj_id=str(org.pk))
    ).order_by("-pgh_created_at", "-pgh_id")


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


def describe(events) -> list[AuditRow]:
    events = list(events)
    user_ids = {e.pgh_context.get("user") for e in events if e.pgh_context and e.pgh_context.get("user")}
    emails = dict(User.objects.filter(pk__in=user_ids).values_list("pk", "email"))
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
                summary=_summary(e.pgh_data or {}),
            )
        )
    return rows
