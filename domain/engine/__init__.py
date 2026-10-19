# SPDX-License-Identifier: AGPL-3.0-or-later
"""The scheduling engine (SPEC §7): generate and rebalance, always as what-ifs.

`run()` never saves anything. It returns one option for a generate, or two for a rebalance
(full fix and limited change, D29), each with the resulting shifts, a diff, per-person hour
changes, the full validation report, status and gap, and explanations. Applying an option is
the caller's job, as a draft (CLAUDE.md §2.2).
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from enum import StrEnum

from domain.engine.candidates import Candidate, Scope, build_candidates
from domain.engine.diff import Change, ChangeKind, HoursChange, hours_changes, match, moved_minutes
from domain.engine.explain import Explanation, explain
from domain.engine.model import build_model
from domain.engine.solve import SolveOutcome, solve
from domain.schedule import ScheduleContext, Shift, minutes_by_person_week
from domain.timeutil import Interval
from domain.validation import Report, validate

__all__ = ["Mode", "Option", "Scope", "SolveRequest", "SolveResult", "run"]


class Mode(StrEnum):
    GENERATE = "generate"
    REBALANCE = "rebalance"


@dataclass(frozen=True)
class SolveRequest:
    mode: Mode
    scope: Scope = field(default_factory=Scope)
    time_limit: float = 30.0  # seconds, covering model building and warm-start completion
    workers: int = 4
    start_step: int = 30  # 30-minute starts halve the problem; 15 stays available (SPEC §7.2)
    length_step: int = 30
    change_limit_minutes: int | None = None  # for the limited-change option
    seed: int = 0


@dataclass
class Option:
    name: str  # "generate", "full" or "limited"
    shifts: list[Shift]
    status: str
    proven_optimal: bool
    gap_slots: float | None
    wall_seconds: float
    kept_existing: bool
    moved_minutes: int
    changes: list[Change]
    hours: list[HoursChange]
    report: Report
    explanation: Explanation
    notes: list[str] = field(default_factory=lambda: [])

    @property
    def status_label(self) -> str:
        if self.kept_existing:
            return "Kept the existing schedule"
        if self.proven_optimal:
            return "Best possible (proven optimal)"
        if self.status == "FEASIBLE":
            gap = (
                f"; at most {self.gap_slots / 4:g} person-hours of over-coverage from the best"
                if self.gap_slots is not None
                else ""
            )
            return f"Best found in the time limit, not proven optimal{gap}"
        return "No schedule found"

    def count(self, kind: ChangeKind) -> int:
        return sum(1 for c in self.changes if c.kind == kind)


@dataclass
class SolveResult:
    mode: Mode
    options: list[Option]
    notes: list[str]
    candidates: int
    dropped_existing: list[Shift]
    wall_seconds: float


def _protected(existing: list[Shift], ctx: ScheduleContext) -> dict[tuple[int, int], int]:
    """People below the median weekly hours keep at least what they had (SPEC §6.4)."""
    weekly = {
        (emp, week): -(-mins // 15)
        for emp, weeks in minutes_by_person_week(existing, ctx.period).items()
        for week, mins in weeks.items()
    }
    if not weekly:
        return {}
    median = statistics.median(weekly.values())
    return {k: v for k, v in weekly.items() if v < median}


def _to_shifts(chosen: list[Candidate]) -> list[Shift]:
    return [
        Shift(
            c.existing_id if c.fixed else None, c.employee_id, c.day, Interval(c.start, c.end), locked=c.fixed
        )
        for c in chosen
    ]


def _option(
    name: str,
    ctx: ScheduleContext,
    req: SolveRequest,
    outcome: SolveOutcome,
    existing: list[Shift],
    open_shifts: list[Shift],
) -> Option:
    notes: list[str] = []
    kept_existing = False
    result: list[Shift]
    if not outcome.ok:
        result = list(existing)
        kept_existing = True
        notes.append("The solver found no schedule in the time limit, so the existing schedule is unchanged.")
    elif (
        req.mode == Mode.REBALANCE
        and outcome.baseline is not None
        and outcome.objective is not None
        and outcome.objective >= outcome.baseline - 1e-6
    ):
        # Never worse (D42, scenario 22): keep the starting schedule unless the solver beat it.
        result = list(existing)
        kept_existing = True
        notes.append(
            "The solver couldn't beat the existing schedule within the time limit, so it's kept as it is."
            if not outcome.proven_optimal
            else "The existing schedule is already the best possible under these rules, so it's kept as it is."
        )
    else:
        chosen = _to_shifts(outcome.chosen)
        fixed = [s for s in chosen if s.locked]
        free = [s for s in chosen if not s.locked]
        free_existing = [s for s in existing if not any(f.id == s.id for f in fixed)]
        matched, _ = match(free_existing, free)
        by_id: dict[int, Shift] = {s.id: s for s in existing if s.id is not None}
        fixed_full = [by_id.get(s.id, s) if s.id is not None else s for s in fixed]
        result = fixed_full + matched
    full = result + open_shifts
    changed = [s for s in existing if s.employee_id is not None]
    _, changes = match(changed, [s for s in result if s.employee_id is not None])
    report = validate(replace(ctx, shifts=full))
    scope = req.scope
    option = Option(
        name=name,
        shifts=full,
        status=outcome.status,
        proven_optimal=outcome.proven_optimal and not kept_existing,
        gap_slots=outcome.gap_slots,
        wall_seconds=outcome.wall_seconds,
        kept_existing=kept_existing,
        moved_minutes=moved_minutes(changed, result),
        changes=changes,
        hours=hours_changes(changed, result, ctx.period),
        report=report,
        explanation=explain(ctx, full, report, scope, proven=outcome.proven_optimal or kept_existing),
        notes=notes,
    )
    return option


def run(
    ctx: ScheduleContext, req: SolveRequest, on_progress: Callable[[str, float, float], None] | None = None
) -> SolveResult:
    t0 = time.monotonic()
    deadline = t0 + req.time_limit
    assigned = [s for s in ctx.shifts if s.employee_id is not None]
    open_shifts = [s for s in ctx.shifts if s.employee_id is None]
    scope = req.scope
    notes: list[str] = []

    if req.mode == Mode.GENERATE:
        # Generate builds from scratch: existing drafts are replaced, but locked, frozen and
        # out-of-scope shifts stay (SPEC §7.3).
        keep_as_is = [s for s in assigned if scope.shift_is_fixed(s)]
        cset = build_candidates(ctx, scope, keep_as_is, req.start_step, req.length_step)
        built = build_model(ctx, cset.candidates)
        outcome = solve(built, deadline, req.workers, req.seed, _progress(on_progress, "generate"))
        option = _option("generate", ctx, req, outcome, assigned, open_shifts)
        return SolveResult(req.mode, [option], notes, len(cset), cset.dropped_existing, time.monotonic() - t0)

    cset = build_candidates(ctx, scope, assigned, req.start_step, req.length_step)
    if cset.dropped_existing:
        notes.append(
            f"{len(cset.dropped_existing)} existing shift(s) no longer fit availability or opening hours and must move."
        )
    protect = _protected(assigned, ctx)
    full_model = build_model(ctx, cset.candidates, existing=assigned, keep=True, protect=protect)
    limited_model = None
    if req.change_limit_minutes is not None:
        limited_model = build_model(
            ctx,
            cset.candidates,
            existing=assigned,
            keep=True,
            protect=protect,
            max_moved_minutes=req.change_limit_minutes,
        )
    if limited_model is None:
        outcomes = [solve(full_model, deadline, req.workers, req.seed, _progress(on_progress, "full"))]
    else:
        # Both options in parallel on half the CPUs each, under one deadline (ADR 0001).
        half = max(1, req.workers // 2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(solve, full_model, deadline, half, req.seed, _progress(on_progress, "full")),
                pool.submit(
                    solve, limited_model, deadline, half, req.seed, _progress(on_progress, "limited")
                ),
            ]
            outcomes = [f.result() for f in futures]
    options = [_option("full", ctx, req, outcomes[0], assigned, open_shifts)]
    if len(outcomes) == 2:
        limited = _option("limited", ctx, req, outcomes[1], assigned, open_shifts)
        full = options[0]
        if req.change_limit_minutes is not None and full.moved_minutes <= req.change_limit_minutes:
            notes.append("The full fix is already within the change limit, so there's one option.")
        elif _same(full, limited):
            notes.append("Both options came out the same, so there's one option.")
        else:
            options.append(limited)
    return SolveResult(req.mode, options, notes, len(cset), cset.dropped_existing, time.monotonic() - t0)


def _same(a: Option, b: Option) -> bool:
    def key(o: Option) -> set[tuple[int | None, object, Interval]]:
        return {(s.employee_id, s.day, s.interval) for s in o.shifts}

    return key(a) == key(b)


def _progress(
    cb: Callable[[str, float, float], None] | None, name: str
) -> Callable[[float, float], None] | None:
    if cb is None:
        return None
    return lambda seconds, objective: cb(name, seconds, objective)
