# SPDX-License-Identifier: AGPL-3.0-or-later
"""Retention (SPEC §11): purge each kind of personal data a set number of days after it stops
being needed. Off until an administrator sets a period. A purge deletes the live rows and
keeps their audit history; the audit log has its own period, after which its entries are
purged too (D109). Counts only (never personal data) are recorded on the policy and in the log."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

import pghistory
from django.apps import apps
from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone
from pghistory.models import Event

from app.core.models import Organization, RetentionPolicy

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Rule:
    field: str
    label: str
    select: Callable[[Organization, date], QuerySet]  # rows due on `cutoff`
    purge: Callable[[QuerySet], int]


def _model(label: str):
    return apps.get_model(label)


def _availability(org, cutoff):
    return _model("availability.Availability").objects.filter(organization=org, term__end_date__lt=cutoff)


def _purge_rows(qs) -> int:
    """Delete the live rows. Their audit history stays until the audit period (D109)."""
    ids = list(qs.values_list("pk", flat=True))
    qs.delete()
    return len(ids)


def _time_off(org, cutoff):
    return _model("availability.TimeOff").objects.filter(organization=org, end_date__lt=cutoff)


def _imports(org, cutoff):
    return (
        _model("imports.ImportBatch")
        .objects.filter(organization=org, created_at__date__lt=cutoff)
        .exclude(payload=[], plan={}, undo={})
    )


def _purge_imports(qs) -> int:
    """Keep the batch record (who imported what file, when), drop the rows it held. An
    import whose data is gone can no longer be undone."""
    return qs.update(payload=[], plan={}, undo={})


def _notifications(org, cutoff):
    return _model("scheduling.Notification").objects.filter(organization=org, created_at__date__lt=cutoff)


def _delete(qs) -> int:
    return qs.delete()[0]


def _solve_runs(org, cutoff):
    return _model("scheduling.SolveRun").objects.filter(organization=org, finished_at__date__lt=cutoff)


# Suggested periods in days (D105): shown next to each field and applied only when an admin
# clicks "Use suggested periods" and then saves. SPEC §11 gives the availability example.
SUGGESTED = {
    "availability_days": 365,
    "time_off_days": 365,
    "import_days": 30,
    "notification_days": 180,
    "solve_run_days": 90,
}

RULES = (
    Rule("availability_days", "Term availability", _availability, _purge_rows),
    Rule("time_off_days", "Time-off requests", _time_off, _purge_rows),
    Rule("import_days", "Import data", _imports, _purge_imports),
    Rule("notification_days", "In-app notifications", _notifications, _delete),
    Rule("solve_run_days", "Solver runs", _solve_runs, _purge_rows),
)
AUDIT_LABEL = "Audit log"


def _audit_events(org: Organization, cutoff: date) -> list[QuerySet]:
    """The organization's audit entries recorded before `cutoff`, one queryset per event
    table. Entries for a model without an organization (a supervisor's department link, say)
    are kept: they hold no student data."""
    out = []
    for model in apps.get_models():
        if not issubclass(model, Event):
            continue
        fields = {f.name for f in model._meta.get_fields()}
        if "organization" in fields:
            mine = model.objects.filter(organization_id=org.pk)
        elif model._meta.label == "core.OrganizationEvent":
            mine = model.objects.filter(pgh_obj_id=org.pk)
        else:
            continue
        out.append(mine.filter(pgh_created_at__date__lt=cutoff))
    return out


def _audit_due(org: Organization, cutoff: date) -> int:
    return sum(qs.count() for qs in _audit_events(org, cutoff))


def _purge_audit(org: Organization, cutoff: date) -> int:
    return sum(qs.delete()[0] for qs in _audit_events(org, cutoff))


@dataclass(frozen=True)
class Due:
    label: str
    days: int | None
    count: int


def policy_for(org: Organization) -> RetentionPolicy:
    return RetentionPolicy.objects.get_or_create(organization=org)[0]


def preview(org: Organization, today: date | None = None) -> list[Due]:
    """What a purge would remove today, without removing anything."""
    today = today or timezone.localdate()
    policy = policy_for(org)
    out = []
    for rule in RULES:
        days = getattr(policy, rule.field)
        count = rule.select(org, today - timedelta(days=days)).count() if days else 0
        out.append(Due(rule.label, days, count))
    days = policy.audit_days
    out.append(Due(AUDIT_LABEL, days, _audit_due(org, today - timedelta(days=days)) if days else 0))
    return out


def purge(org: Organization, today: date | None = None) -> dict[str, int]:
    today = today or timezone.localdate()
    counts: dict[str, int] = {}
    with transaction.atomic(), pghistory.context(action="retention_purge"):
        policy = RetentionPolicy.objects.select_for_update().get(pk=policy_for(org).pk)
        for rule in RULES:
            days = getattr(policy, rule.field)
            if days:
                counts[rule.label] = rule.purge(rule.select(org, today - timedelta(days=days)))
        if policy.audit_days:  # last, so it also covers entries the purges above just wrote
            counts[AUDIT_LABEL] = _purge_audit(org, today - timedelta(days=policy.audit_days))
        policy.last_purged_at = timezone.now()
        policy.last_purge_counts = counts
        policy.save(update_fields=["last_purged_at", "last_purge_counts"])
    logger.info("retention purge", extra={"organization_id": org.pk, "counts": counts})
    return counts


def purge_all(today: date | None = None) -> dict[int, dict[str, int]]:
    return {
        p.organization_id: purge(p.organization, today)
        for p in RetentionPolicy.objects.select_related("organization")
        if p.audit_days or any(getattr(p, r.field) for r in RULES)
    }
