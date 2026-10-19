# SPDX-License-Identifier: AGPL-3.0-or-later
# pyright: basic
"""Running CP-SAT with the settings the benchmark chose (ADR 0001): presolve off, the warm
start completed to a full assignment, a shared deadline that covers hint completion, and
an honest status and gap. A time-limited result is never presented as optimal (CLAUDE.md §4)."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ortools.sat.python import cp_model

from domain.engine.candidates import Candidate
from domain.engine.model import W_OVER, Built

SOLVER_PARAMS = {"cp_model_presolve": False}


@dataclass
class SolveOutcome:
    status: str  # OPTIMAL, FEASIBLE, INFEASIBLE, UNKNOWN or MODEL_INVALID
    chosen: list[Candidate] = field(default_factory=lambda: [])
    objective: float | None = None
    bound: float | None = None
    baseline: float | None = None  # objective of the starting schedule, when there was one
    wall_seconds: float = 0.0
    tier_values: dict[str, float] = field(default_factory=lambda: {})
    kept_slots: list[int] = field(default_factory=lambda: [])  # per free existing shift

    @property
    def ok(self) -> bool:
        return self.status in ("OPTIMAL", "FEASIBLE")

    @property
    def proven_optimal(self) -> bool:
        return self.status == "OPTIMAL"

    @property
    def gap_slots(self) -> float | None:
        """Distance from the best possible, in over-coverage person-slots (15 minutes each). A
        relative gap means nothing here because keep-existing terms push objectives negative."""
        if self.objective is None or self.bound is None:
            return None
        return abs(self.objective - self.bound) / W_OVER


def complete_hint(built: Built, workers: int, time_limit: float) -> float | None:
    """Turn the existing schedule into a full assignment and hint it (ADR 0001: hinting only the
    shift choices left CP-SAT unable to use it). Returns the starting schedule's objective, or
    None if it breaks a hard constraint under the current rules."""
    if not built.hint_assignment:
        return None
    fixed = built.model.clone()
    for i, value in built.hint_assignment.items():
        fixed.add(fixed.get_bool_var_from_proto_index(built.x[i].index) == value)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(0.5, time_limit)
    solver.parameters.num_workers = max(1, workers)
    status = solver.solve(fixed)
    built.model.clear_hints()
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        for i in range(len(built.model.proto.variables)):
            built.model.add_hint(
                built.model.get_int_var_from_proto_index(i),
                solver.value(fixed.get_int_var_from_proto_index(i)),
            )
        return solver.objective_value
    for i, value in built.hint_assignment.items():
        built.model.add_hint(built.x[i], value)
    return None


def solve(
    built: Built,
    deadline: float,
    workers: int,
    seed: int = 0,
    on_progress: Callable[[float, float], None] | None = None,
) -> SolveOutcome:
    """Solve until `deadline` (a `time.monotonic()` value)."""
    t0 = time.monotonic()
    baseline = complete_hint(built, workers, min(10.0, max(0.5, (deadline - t0) / 3)))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(0.5, deadline - time.monotonic())
    solver.parameters.num_workers = max(1, workers)
    solver.parameters.random_seed = seed
    for k, v in SOLVER_PARAMS.items():
        setattr(solver.parameters, k, v)

    class Progress(cp_model.CpSolverSolutionCallback):
        def on_solution_callback(self) -> None:
            if on_progress is not None:
                on_progress(self.wall_time, self.objective_value)

    status = solver.solve(built.model, Progress())
    outcome = SolveOutcome(solver.status_name(status), baseline=baseline, wall_seconds=time.monotonic() - t0)
    if not outcome.ok:
        return outcome
    outcome.chosen = [c for c, v in zip(built.cands, built.x, strict=True) if solver.value(v)]
    outcome.objective = solver.objective_value
    outcome.bound = solver.best_objective_bound
    outcome.tier_values = {
        tier: float(sum(coef * solver.value(var) for coef, var in terms))
        for tier, terms in built.terms.items()
    }
    outcome.kept_slots = [int(solver.value(k)) if not isinstance(k, int) else k for _, k in built.kept_exprs]
    return outcome
