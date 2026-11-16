# SPDX-License-Identifier: AGPL-3.0-or-later
"""Running the engine from the app: queue a solve, execute it in the worker, store the options,
and apply one as draft changes in a single transaction with an undo (SPEC §7.3, §9.3)."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, timedelta
from typing import Any

import pghistory
from django.conf import settings
from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from app.core.timezones import local_now, org_time
from app.people.models import Employee
from app.scheduling import services
from app.scheduling.models import RunStatus, SchedulePeriod, SchedulingRules, Shift, SolveRun
from domain import engine
from domain.engine import Mode, Option, Scope, SolveRequest
from domain.engine.diff import ChangeKind
from domain.engine.explain import WHAT_IF_LABELS
from domain.schedule import ScheduleContext
from domain.timeutil import Interval

log = logging.getLogger(__name__)

# A run still "running" this long after its time limit has been abandoned: the worker stopped
# (out of memory, a restart or an upgrade) without recording an outcome (D126). The time limit
# covers building and solving the model; the grace covers loading the period before that clock
# starts and writing up the options after it stops, with room for a slow or busy server.
ABANDONED_GRACE = timedelta(minutes=5)
# A run still waiting to start after this long gets a plain note on its page. It is never failed
# for waiting: it starts once the worker is free or back (D126).
WAITING_NOTE_AFTER = timedelta(minutes=2)
ABANDONED_ERROR = (
    "The solve stopped before it finished, so there are no options to compare: the worker running it "
    "was probably restarted or ran out of memory. The schedule is unchanged. Try again with fewer days "
    '(under Advanced, set "Only change from" and "Only change until"). If it stops again, ask IT to '
    "check the worker's memory limit (WORKER_MEMORY in docs/install.md)."
)


class SolveError(Exception):
    pass


def time_limit_seconds(mode: str) -> int:
    return settings.STACKS_GENERATE_SECONDS if mode == Mode.GENERATE else settings.STACKS_REBALANCE_SECONDS


def longest_run() -> timedelta:
    """How long a run can be "running" before it counts as abandoned, for the longer mode."""
    return timedelta(seconds=max(time_limit_seconds(m) for m in Mode)) + ABANDONED_GRACE


def fail_abandoned(runs: QuerySet[SolveRun]) -> int:
    """Mark as failed each of `runs` still running well past its time limit (D126), and return
    how many. Each mark is one conditional UPDATE, so a run whose worker has just recorded its
    outcome is left alone. The schedule is never touched."""
    now = timezone.now()
    marked = 0
    for mode in Mode:
        cutoff = now - timedelta(seconds=time_limit_seconds(mode)) - ABANDONED_GRACE
        marked += runs.filter(status=RunStatus.RUNNING, mode=mode, started_at__lt=cutoff).update(
            status=RunStatus.FAILED, error=ABANDONED_ERROR, finished_at=now
        )
    if marked:
        log.warning("Marked %d solve run(s) failed: still running long after the time limit", marked)
    return marked


def working_copy_hash(period: SchedulePeriod) -> str:
    rows = sorted(
        (s.pk, s.employee_id or 0, s.date.isoformat(), s.start_min, s.end_min) for s in period.shifts.live()
    )
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def start_solve(period: SchedulePeriod, user, mode: str, params: dict[str, Any]) -> SolveRun:
    """Queue a solve. It runs in the worker after this transaction commits."""
    params = {**params, "start_hash": working_copy_hash(period)}
    run = SolveRun.objects.create(
        organization=period.organization, period=period, mode=mode, params=params, created_by=user
    )
    if settings.STACKS_SOLVE_INLINE:
        transaction.on_commit(lambda: execute_run(run.pk))
    else:
        from app.scheduling.tasks import run_solve

        transaction.on_commit(lambda: run_solve.defer(run_id=run.pk))
    return run


def _freeze_until(org, hours: int) -> tuple[date, int] | None:
    if hours <= 0:
        return None
    now = local_now(org) + timedelta(hours=hours)
    return (now.date(), now.hour * 60 + now.minute)


def build_request(run: SolveRun) -> tuple[ScheduleContext, SolveRequest]:
    period = run.period
    org = period.organization
    p = run.params
    rules = SchedulingRules.for_org(org)
    ctx = services.build_context(period)
    locked: frozenset[int] = frozenset()
    latest = period.latest_version()
    if latest is not None and not p.get("allow_published"):
        locked = frozenset(r["id"] for r in latest.shifts)  # D30: published shifts stay put
    freeze_hours = p.get("freeze_hours", rules.freeze_window_hours)
    scope = Scope(
        first_day=date.fromisoformat(p["first_day"]) if p.get("first_day") else None,
        last_day=date.fromisoformat(p["last_day"]) if p.get("last_day") else None,
        people=frozenset(p["people"]) if p.get("people") else None,
        locked_ids=locked,
        freeze_until=_freeze_until(org, int(freeze_hours)),
    )
    scheduled = sum(s.minutes for s in ctx.shifts if s.employee_id is not None and scope.day_in_scope(s.day))
    limit_pct = int(p.get("change_limit_percent", rules.change_limit_percent))
    mode = Mode(run.mode)
    request = SolveRequest(
        mode=mode,
        scope=scope,
        time_limit=float(time_limit_seconds(mode)),
        workers=settings.STACKS_SOLVER_WORKERS,
        change_limit_minutes=scheduled * limit_pct // 100 if mode == Mode.REBALANCE else None,
    )
    if p.get("relax_tag_rule"):  # one hard rule becomes a preference for this run (D97)
        from dataclasses import replace

        ctx = replace(ctx, rules=ctx.rules.softened(p["relax_tag_rule"]))
    elif p.get("relax_tag_rules"):
        from dataclasses import replace

        ctx = replace(ctx, rules=replace(ctx.rules, tag_rules=[]))
    return ctx, request


def execute_run(run_id: int) -> None:
    """Run in the worker, in the organization's time zone (D119). Records progress, then the
    options or the failure."""
    run = SolveRun.objects.select_related("period__organization", "period__location").get(pk=run_id)
    with org_time(run.period.organization):
        _execute(run)


def _execute(run: SolveRun) -> None:
    started = timezone.now()
    # Only a queued run starts, so running the job again for a run that has started, finished or
    # been given up (a retried stalled job, a second worker) does nothing (D126).
    claimed = SolveRun.objects.filter(pk=run.pk, status=RunStatus.QUEUED).update(
        status=RunStatus.RUNNING, started_at=started
    )
    if not claimed:
        return
    run.status, run.started_at = RunStatus.RUNNING, started
    outcome: dict[str, Any]
    try:
        ctx, request = build_request(run)
        last = {"t": 0.0}

        def progress(name: str, seconds: float, objective: float) -> None:
            if seconds - last["t"] >= 2:  # don't write to the database on every improvement
                last["t"] = seconds
                SolveRun.objects.filter(pk=run.pk, status=RunStatus.RUNNING).update(
                    progress={"option": name, "seconds": round(seconds, 1), "objective": objective}
                )

        result = engine.run(ctx, request, progress)
        names = {pid: p.name for pid, p in ctx.people.items()}
        outcome = {
            "status": RunStatus.DONE,
            "error": "",
            "result": {
                "notes": result.notes,
                "candidates": result.candidates,
                "wall_seconds": round(result.wall_seconds, 1),
                "change_limit_minutes": request.change_limit_minutes,
                "options": [serialize_option(o, names) for o in result.options],
            },
        }
    except Exception as exc:  # report the failure on the run page; never lose the working copy
        log.exception("Solve run %s failed", run.pk)
        outcome = {
            "status": RunStatus.FAILED,
            "error": f"{exc.__class__.__name__}: the solve couldn't finish. The schedule is unchanged.",
        }
    # The worker's outcome wins (D126): if a page gave up on this run as abandoned while it was
    # still solving, the options replace the "stopped" message, and a supervisor still has to
    # apply one. Matching started_at means only the worker that started the run records it.
    recorded = SolveRun.objects.filter(
        pk=run.pk, started_at=started, status__in=[RunStatus.RUNNING, RunStatus.FAILED]
    ).update(finished_at=timezone.now(), **outcome)
    if not recorded:
        log.warning("Solve run %s changed while it was solving; its outcome was not recorded", run.pk)


def serialize_option(o: Option, names: dict[int, str]) -> dict[str, Any]:
    return {
        "name": o.name,
        "status": o.status,
        "status_label": o.status_label,
        "proven_optimal": o.proven_optimal,
        "gap_hours": round(o.gap_slots / 4, 1) if o.gap_slots is not None and not o.proven_optimal else None,
        "kept_existing": o.kept_existing,
        "moved_minutes": o.moved_minutes,
        "notes": o.notes,
        "counts": {k.value: o.count(k) for k in ChangeKind},
        "summary": o.report.summary(),
        "issues": [
            {"category": i.category.value, "severity": i.severity.value, "message": i.message}
            for i in o.report.issues
        ],
        "hours": [
            {
                "name": names.get(h.employee_id, f"#{h.employee_id}"),
                "before": list(h.before),
                "after": list(h.after),
                "delta": h.delta,
            }
            for h in o.hours
        ],
        "people": [
            {"name": names.get(pid, f"#{pid}"), "text": text} for pid, text in o.explanation.people.items()
        ],
        "trade_offs": o.explanation.trade_offs,
        "unmet": [
            {
                "message": u.message,
                "reason": u.reason,
                "rule": u.rule,
                "what_ifs": [{"key": w, "label": WHAT_IF_LABELS[w]} for w in u.what_ifs],
            }
            for u in o.explanation.unmet
        ],
        "shifts": [
            {
                "id": s.id,
                "employee_id": s.employee_id,
                "date": s.day.isoformat(),
                "start": s.interval.start,
                "end": s.interval.end,
            }
            for s in o.shifts
        ],
    }


def _snapshot(period: SchedulePeriod) -> list[dict[str, Any]]:
    return [
        {**s.snapshot(), "deleted_at": s.deleted_at.isoformat() if s.deleted_at else None}
        for s in period.shifts.all()
    ]


def apply_option(run: SolveRun, name: str, user) -> int:
    """Write an option into the working copy as drafts, in one transaction under one audit
    context, keeping shift IDs for retimes and reassignments. Returns the number of shifts
    changed. Refuses if the schedule changed since the solve started."""
    with transaction.atomic(), pghistory.context(solve_run=run.pk, action="apply_option"):
        run = SolveRun.objects.select_for_update().get(pk=run.pk)
        period = run.period
        if run.status != RunStatus.DONE:
            raise SolveError("This run can't be applied.")
        option = next((o for o in run.result.get("options", []) if o["name"] == name), None)
        if option is None:
            raise SolveError("Unknown option.")
        if working_copy_hash(period) != run.params.get("start_hash"):
            raise SolveError(
                "The schedule changed after this solve started. Run it again to get current options."
            )
        run.undo = {"shifts": _snapshot(period)}
        current = {s.pk: s for s in period.shifts.live()}
        defaults = dict(
            Employee.objects.filter(organization=period.organization).values_list("pk", "default_pay_role_id")
        )
        seen: set[int] = set()
        touched = 0
        now = timezone.now()
        for row in option["shifts"]:
            day = date.fromisoformat(row["date"])
            if row["id"] is not None and row["id"] in current:
                s = current[row["id"]]
                seen.add(s.pk)
                new_values = {
                    "employee_id": row["employee_id"],
                    "date": day,
                    "start_min": row["start"],
                    "end_min": row["end"],
                }
                if any(getattr(s, k) != v for k, v in new_values.items()):
                    if s.employee_id != row["employee_id"] and row["employee_id"] is not None:
                        s.pay_role_id = defaults.get(row["employee_id"])  # scenario 15
                    for k, v in new_values.items():
                        setattr(s, k, v)
                    s.updated_by = user
                    s.proposed_by_lead = False
                    s.save()
                    touched += 1
            elif row["id"] is None:
                Shift.objects.create(
                    organization=period.organization,
                    location=period.location,
                    period=period,
                    employee_id=row["employee_id"],
                    date=day,
                    start_min=row["start"],
                    end_min=row["end"],
                    pay_role_id=defaults.get(row["employee_id"]) if row["employee_id"] else None,
                    updated_by=user,
                )
                touched += 1
        for pk, s in current.items():
            if pk not in seen and s.employee_id is not None:
                s.deleted_at = now  # soft delete: restorable (CLAUDE.md §2.3)
                s.updated_by = user
                s.save()
                touched += 1
        run.status = RunStatus.APPLIED
        run.applied_option = name
        run.applied_at = now
        run.save()
    return touched


def undo_apply(run: SolveRun, user) -> None:
    with transaction.atomic(), pghistory.context(solve_run=run.pk, action="undo_apply"):
        run = SolveRun.objects.select_for_update().get(pk=run.pk)
        if run.status != RunStatus.APPLIED:
            raise SolveError("Only an applied run can be undone.")
        later = SolveRun.objects.filter(
            period=run.period, status=RunStatus.APPLIED, applied_at__gt=run.applied_at
        )
        if later.exists():
            raise SolveError("A later run has been applied since. Undo that one first.")
        before = {r["id"]: r for r in run.undo.get("shifts", [])}
        for s in run.period.shifts.all():
            row = before.get(s.pk)
            if row is None:
                if s.deleted_at is None:
                    s.deleted_at = timezone.now()  # created by the apply
                    s.updated_by = user
                    s.save()
                continue
            s.employee_id = row["employee_id"]
            s.date = date.fromisoformat(row["date"])
            s.start_min, s.end_min = row["start"], row["end"]
            s.pay_role_id = row["pay_role_id"]
            s.deleted_at = datetime.fromisoformat(row["deleted_at"]) if row["deleted_at"] else None
            s.updated_by = user
            s.save()
        run.status = RunStatus.UNDONE
        run.save()


def option_interval(row: dict[str, Any]) -> Interval:
    return Interval(row["start"], row["end"])
