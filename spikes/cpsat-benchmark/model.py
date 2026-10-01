"""Candidate-shift scheduling model (SPEC §7.2), emitted to CP-SAT or to a MIP solver.

The same model is built through a thin backend interface so the benchmark compares
solvers on identical formulations: OR-Tools CP-SAT directly, and CBC or HiGHS via PuLP.

This is spike code for the stack ADR, not the production engine.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict
from dataclasses import dataclass

from synth import (
    FRI,
    MON,
    OPERATING_INTERVALS,
    PERIOD_DAYS,
    SLOTS_PER_HOUR,
    Roster,
    open_slots,
    opening_slot,
    slot,
    week_of,
    weekday_of,
)

# Objective tiers (SPEC §7.2, CLAUDE.md §4). Units are per 15-minute slot unless noted.
# Each tier is set so a plausible amount of a lower tier can't buy one unit of the tier above.
W_SHORTFALL = 1_000_000  # uncovered person-slot: always the most expensive thing
W_TAG = 100_000  # soft tag coverage rule miss (§6.3)
W_NOT_ENTERED = 50_000  # using someone whose availability is not_entered (§5.1)
W_OVER = 10_000  # over-coverage person-slot
W_TARGET = 1_000  # per slot short of an hour target, times the target priority
W_ROTATION = 300  # off-week slot under `preferred` rotation
W_KEEP_REBALANCE = 400  # per kept slot in rebalance: above fairness so small edits give small diffs
W_FAIR = 100  # per slot of the highest weekly load among general staff
W_PROTECT = 100  # per slot cut from a low-hour person when rebalancing
W_SHIFT = 20  # per shift: fewer, longer shifts
W_PREF = 1  # bonus per slot worked inside a preferred block


@dataclass(frozen=True)
class Candidate:
    emp: str
    day: int
    start: int
    end: int
    duty: bool = False

    @property
    def length(self) -> int:
        return self.end - self.start

    @property
    def key(self) -> tuple:
        return (self.emp, self.day, self.start, self.end)


def _overlaps(a: int, b: int, ranges: list[tuple[int, int]]) -> bool:
    return any(a < rb and ra < b for ra, rb in ranges)


def _in_group_week(group: str | None, day: int) -> bool:
    return group is None or (group == "A") == (week_of(day) == 0)


def build_candidates(roster: Roster, start_step: int = 2, length_step: int = 2) -> list[Candidate]:
    """Every start on the step grid and every allowed length that fits inside an open
    interval (so never across a closure) and avoids `unavailable` blocks (SPEC §7.2)."""
    r = roster.rules
    cands: list[Candidate] = []
    for e in roster.employees:
        if e.employment_type != "regular":
            continue  # non_regular are never auto-scheduled (scenario 7)
        for day in range(PERIOD_DAYS):
            if r.rotation == "strict" and not _in_group_week(e.rotation_group, day):
                continue
            wd = weekday_of(day)
            blocked = e.unavailable.get(wd, [])
            for a, b in OPERATING_INTERVALS[wd]:
                for s in range(a, b, start_step):
                    for length in range(r.min_len, r.max_len + 1, length_step):
                        if s + length > b:
                            break
                        if not _overlaps(s, s + length, blocked):
                            cands.append(Candidate(e.id, day, s, s + length))
    d = roster.duties
    if d.opener:
        op = roster.get(d.opener)
        for day in range(PERIOD_DAYS):
            wd = weekday_of(day)
            if wd in d.opener_short_weekdays:
                s = opening_slot(wd)
                e = s + d.opener_short_slots
                if not _overlaps(s, e, op.unavailable.get(wd, [])):
                    cands.append(Candidate(op.id, day, s, e, duty=True))
    return cands


def _pref_slots(roster: Roster, c: Candidate) -> int:
    pref = roster.get(c.emp).preferred.get(weekday_of(c.day), [])
    return sum(max(0, min(c.end, b) - max(c.start, a)) for a, b in pref)


# ---------------------------------------------------------------- backends


class CpSatBackend:
    name = "cpsat"

    def __init__(self) -> None:
        from ortools.sat.python import cp_model

        self.cp_model = cp_model
        self.model = cp_model.CpModel()
        self.hints: list[tuple] = []

    def bool(self, name: str):
        return self.model.new_bool_var(name)

    def int(self, lb: int, ub: int, name: str):
        return self.model.new_int_var(lb, ub, name)

    def expr(self, terms):
        return self.cp_model.LinearExpr.weighted_sum([v for _, v in terms], [c for c, _ in terms])

    def add(self, constraint) -> None:
        self.model.add(constraint)

    def at_most_one(self, vs) -> None:
        self.model.add_at_most_one(vs)

    def minimize(self, terms) -> None:
        self.model.minimize(self.expr(terms))

    def hint(self, v, value: int) -> None:
        # Stored, then completed to a full assignment in solve_cpsat (see complete_hint).
        self.hints.append((v, value))


class PulpBackend:
    def __init__(self, name: str) -> None:
        import pulp

        self.pulp = pulp
        self.name = name
        self.prob = pulp.LpProblem("schedule", pulp.LpMinimize)
        self.has_hints = False

    def bool(self, name: str):
        return self.pulp.LpVariable(name, cat="Binary")

    def int(self, lb: int, ub: int, name: str):
        return self.pulp.LpVariable(name, lowBound=lb, upBound=ub, cat="Integer")

    def expr(self, terms):
        return self.pulp.LpAffineExpression([(v, c) for c, v in terms])

    def add(self, constraint) -> None:
        self.prob += constraint

    def at_most_one(self, vs) -> None:
        self.prob += self.pulp.lpSum(vs) <= 1

    def minimize(self, terms) -> None:
        self.prob.setObjective(self.expr(terms))

    def hint(self, v, value: int) -> None:
        v.setInitialValue(value)
        self.has_hints = True


# ---------------------------------------------------------------- model


@dataclass
class Built:
    backend: object
    cands: list[Candidate]
    x: list
    unders: list
    n_constraints: int
    build_seconds: float


def build_model(
    roster: Roster,
    backend,
    cands: list[Candidate],
    existing: set[tuple] | None = None,
    keep_weight: int = 0,
    protect: dict[tuple[str, int], int] | None = None,
) -> Built:
    t0 = time.perf_counter()
    r, d = roster.rules, roster.duties
    emps = {e.id: e for e in roster.employees}
    n_cons = 0

    def add(c):
        nonlocal n_cons
        backend.add(c)
        n_cons += 1

    x = [backend.bool(f"x{i}") for i in range(len(cands))]
    obj: list[tuple[int, object]] = []

    by_person_day: dict[tuple, list[int]] = defaultdict(list)
    by_person_week: dict[tuple, list[int]] = defaultdict(list)
    by_cover: dict[tuple, list[int]] = defaultdict(list)
    for i, c in enumerate(cands):
        by_person_day[(c.emp, c.day)].append(i)
        by_person_week[(c.emp, week_of(c.day))].append(i)
        for t in range(c.start, c.end):
            by_cover[(c.day, t)].append(i)

    # Coverage (§6.2): cover + under - over == target on every open slot.
    unders = []
    slack: dict[tuple, tuple] = {}
    for day in range(PERIOD_DAYS):
        for t in open_slots(weekday_of(day)):
            u = backend.int(0, r.coverage_target, f"u_{day}_{t}")
            o = backend.int(0, 20, f"o_{day}_{t}")
            add(backend.expr([(1, x[i]) for i in by_cover[(day, t)]] + [(1, u), (-1, o)]) == r.coverage_target)
            unders.append(u)
            slack[(day, t)] = (u, o)
            obj.append((W_SHORTFALL, u))
            if r.coverage_mode == "exact":
                obj.append((W_OVER, o))

    # Shift shape per person-day (§6.1).
    for (_emp, _day), idxs in by_person_day.items():
        if len(idxs) > r.max_shifts_per_day:
            add(backend.expr([(1, x[i]) for i in idxs]) <= r.max_shifts_per_day)
        if sum(cands[i].length for i in idxs) > r.max_day_slots:
            add(backend.expr([(cands[i].length, x[i]) for i in idxs]) <= r.max_day_slots)
        # No overlap and a minimum gap: at most one shift, extended by the gap, covers any slot.
        ext: dict[int, list[int]] = defaultdict(list)
        for i in idxs:
            for t in range(cands[i].start, cands[i].end + r.min_gap):
                ext[t].append(i)
        seen = set()
        for group in ext.values():
            key = tuple(group)
            if len(group) > 1 and key not in seen:
                seen.add(key)
                backend.at_most_one([x[i] for i in group])
                n_cons += 1

    # Caps (§6.4).
    lead_weeks = dict(d.leads)
    for (emp, week), idxs in by_person_week.items():
        terms = [(cands[i].length, x[i]) for i in idxs]
        cap = int(emps[emp].weekly_cap * SLOTS_PER_HOUR)
        if emp not in lead_weeks and sum(c for c, _ in terms) > cap:
            add(backend.expr(terms) <= cap)
    for emp, lead_week in lead_weeks.items():  # scenario 4: 20h in the lead week, at most 10h otherwise
        for week in (0, 1):
            terms = [(cands[i].length, x[i]) for i in by_person_week[(emp, week)]]
            if week == lead_week:
                add(backend.expr(terms) == int(d.lead_week_hours * SLOTS_PER_HOUR))
            else:
                add(backend.expr(terms) <= int(d.lead_off_week_max * SLOTS_PER_HOUR))
    for e in roster.employees:
        if e.period_cap is not None:
            idxs = [i for i, c in enumerate(cands) if c.emp == e.id]
            add(backend.expr([(cands[i].length, x[i]) for i in idxs]) <= int(e.period_cap * SLOTS_PER_HOUR))

    # Opener duty (scenario 3): a shift starting at opening time every weekday.
    if d.opener:
        for day in range(PERIOD_DAYS):
            wd = weekday_of(day)
            if MON <= wd <= FRI:
                idxs = [
                    i
                    for i in by_person_day[(d.opener, day)]
                    if cands[i].start == opening_slot(wd) and (cands[i].duty or wd not in d.opener_short_weekdays)
                ]
                if idxs:
                    add(backend.expr([(1, x[i]) for i in idxs]) == 1)

    # Tag coverage rules (§6.3), soft with a high weight.
    if r.tag_rules:

        def has(i, tag):
            return tag in emps[cands[i].emp].tags

        for day in range(PERIOD_DAYS):
            wd = weekday_of(day)
            for t in open_slots(wd):
                cover = by_cover[(day, t)]
                # At least one Shift lead on duty during open hours.
                m = backend.bool(f"ml_{day}_{t}")
                add(backend.expr([(1, x[i]) for i in cover if has(i, "Shift lead")] + [(1, m)]) >= 1)
                obj.append((W_TAG, m))
                # At least one Cataloguer on duty Mon-Fri 9am-5pm.
                if MON <= wd <= FRI and slot(9) <= t < slot(17):
                    m = backend.bool(f"mc_{day}_{t}")
                    add(backend.expr([(1, x[i]) for i in cover if has(i, "Cataloguer")] + [(1, m)]) >= 1)
                    obj.append((W_TAG, m))
                # A New employee is never the only person on duty: new <= K * (others + miss).
                # Others = (target - under + over) - new, read from the coverage equation, so the
                # constraint only lists new employees' candidates instead of every covering one.
                new = [i for i in cover if has(i, "New employee")]
                if new:
                    k = 4
                    u, o = slack[(day, t)]
                    m = backend.bool(f"mn_{day}_{t}")
                    terms = [(1 + k, x[i]) for i in new] + [(k, u), (-k, o), (-k, m)]
                    add(backend.expr(terms) <= k * r.coverage_target)
                    obj.append((W_TAG, m))
        # Processor hours total at least 15 per week.
        for week in (0, 1):
            need = 15 * SLOTS_PER_HOUR
            idxs = [i for i, c in enumerate(cands) if week_of(c.day) == week and has(i, "Processor")]
            deficit = backend.int(0, need, f"proc_def_{week}")
            add(backend.expr([(cands[i].length, x[i]) for i in idxs] + [(1, deficit)]) >= need)
            obj.append((W_TAG // SLOTS_PER_HOUR, deficit))

    # Hour targets (§6.4), soft.
    for e in roster.employees:
        if e.target_week_hours is None:
            continue
        need = int(e.target_week_hours * SLOTS_PER_HOUR)
        for week in (0, 1):
            deficit = backend.int(0, need, f"tgt_{e.id}_{week}")
            terms = [(cands[i].length, x[i]) for i in by_person_week[(e.id, week)]]
            add(backend.expr(terms + [(1, deficit)]) >= need)
            obj.append((W_TARGET * e.target_priority, deficit))

    # Fairness: minimise the highest weekly load among general staff.
    special = {d.opener, *lead_weeks}
    general = [
        e
        for e in roster.employees
        if e.employment_type == "regular"
        and e.id not in special
        and e.target_week_hours is None
        and e.availability_status == "entered"
        and "New employee" not in e.tags
    ]
    peak = backend.int(0, 20 * SLOTS_PER_HOUR, "peak")
    for e in general:
        for week in (0, 1):
            terms = [(cands[i].length, x[i]) for i in by_person_week[(e.id, week)]]
            if terms:
                add(backend.expr(terms + [(-1, peak)]) <= 0)
    obj.append((W_FAIR, peak))

    # Protected minimum (rebalance): don't push low-hour people lower.
    for (emp, week), before in (protect or {}).items():
        cut = backend.int(0, before, f"cut_{emp}_{week}")
        terms = [(cands[i].length, x[i]) for i in by_person_week[(emp, week)]]
        add(backend.expr(terms + [(1, cut)]) >= before)
        obj.append((W_PROTECT, cut))

    # Per-candidate terms: not_entered, rotation, keep bonus, shift count, preferences.
    for i, c in enumerate(cands):
        e = emps[c.emp]
        coef = W_SHIFT - W_PREF * _pref_slots(roster, c)
        if e.availability_status == "not_entered":
            coef += W_NOT_ENTERED * c.length
        if r.rotation == "preferred" and not _in_group_week(e.rotation_group, c.day):
            coef += W_ROTATION * c.length
        if existing is not None:
            kept = c.key in existing
            if kept:
                coef -= keep_weight * c.length
            backend.hint(x[i], 1 if kept else 0)
        if coef:
            obj.append((coef, x[i]))

    backend.minimize(obj)
    return Built(backend, cands, x, unders, n_cons, time.perf_counter() - t0)


# ---------------------------------------------------------------- solving


@dataclass
class Result:
    solver: str
    status: str
    objective: float | None
    bound: float | None
    gap: float | None
    wall_seconds: float
    build_seconds: float
    n_vars: int
    n_candidates: int
    n_constraints: int
    timeline: list[tuple[float, float, int]]  # (seconds, objective, shortfall slots)
    shifts: list[Candidate]


def _gap(obj, bound):
    """Optimality gap in units of one over-coverage slot (W_OVER).

    A relative gap is meaningless here: keep-existing bonuses push rebalance objectives
    to zero or below. "gap 3" reads as: the solution is at most 3 over-coverage slots
    (45 person-minutes) worse than the best possible.
    """
    if obj is None or bound is None:
        return None
    return abs(obj - bound) / W_OVER


def complete_hint(built: Built, workers: int, time_limit: float = 10.0) -> float:
    """Turn a partial warm start (shift choices only) into a full assignment.

    Fixing the existing shifts and solving for the slack, miss and deficit variables takes
    a fraction of a second, and hinting every variable lets CP-SAT start the real search
    from the existing schedule instead of rediscovering it (CLAUDE.md §4: warm start).
    Returns the seconds spent.
    """
    backend = built.backend
    cp_model = backend.cp_model
    t0 = time.perf_counter()
    fixed = backend.model.clone()
    for v, value in backend.hints:
        fixed.add(fixed.get_bool_var_from_proto_index(v.index) == value)
    s = cp_model.CpSolver()
    s.parameters.max_time_in_seconds = time_limit
    s.parameters.num_workers = workers
    status = s.solve(fixed)
    backend.model.clear_hints()
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        for i in range(len(backend.model.proto.variables)):
            backend.model.add_hint(backend.model.get_int_var_from_proto_index(i),
                                   s.value(fixed.get_int_var_from_proto_index(i)))
    else:  # existing schedule infeasible under the new rules: hint the shifts only
        for v, value in backend.hints:
            backend.model.add_hint(v, value)
    return time.perf_counter() - t0


def solve_cpsat(built: Built, time_limit: float, workers: int, seed: int = 0, params: dict | None = None) -> Result:
    cp_model = built.backend.cp_model
    t_start = time.perf_counter()
    hint_seconds = complete_hint(built, workers) if built.backend.hints else 0.0
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(1.0, time_limit - hint_seconds)
    solver.parameters.num_workers = workers
    solver.parameters.random_seed = seed
    for k, v in (params or {}).items():
        setattr(solver.parameters, k, v)

    timeline: list[tuple[float, float, int]] = []

    class Progress(cp_model.CpSolverSolutionCallback):
        def on_solution_callback(self):
            shortfall = sum(self.value(u) for u in built.unders)
            timeline.append((self.wall_time, self.objective_value, shortfall))

    status = solver.solve(built.backend.model, Progress())
    wall = time.perf_counter() - t_start
    timeline = [(t + hint_seconds, o, sf) for t, o, sf in timeline]
    ok = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    shifts = [c for c, v in zip(built.cands, built.x) if ok and solver.value(v)]
    obj = solver.objective_value if ok else None
    bound = solver.best_objective_bound if ok else None
    return Result(
        "cpsat",
        solver.status_name(status),
        obj,
        bound,
        _gap(obj, bound),
        wall,
        built.build_seconds,
        len(built.backend.model.proto.variables),
        len(built.cands),
        built.n_constraints,
        timeline,
        shifts,
    )


def solve_pulp(built: Built, time_limit: float, threads: int, log_path: str) -> Result:
    pulp = built.backend.pulp
    prob = built.backend.prob
    if built.backend.name == "cbc":
        solver = pulp.PULP_CBC_CMD(
            msg=True, timeLimit=time_limit, threads=threads, warmStart=built.backend.has_hints, logPath=log_path
        )
    else:
        solver = pulp.HiGHS(msg=False, timeLimit=time_limit, threads=threads)
    t0 = time.perf_counter()
    prob.solve(solver)
    wall = time.perf_counter() - t0
    # PuLP reports "Optimal" even when CBC stops on the time limit; sol_status 1 = optimal, 2 = feasible.
    status = {1: "OPTIMAL", 2: "FEASIBLE", 0: "NO_SOLUTION", -1: "INFEASIBLE", -2: "UNBOUNDED"}.get(
        prob.sol_status, str(prob.sol_status)
    )
    ok = prob.sol_status in (1, 2)
    obj = pulp.value(prob.objective) if ok else None
    bound = None
    if built.backend.name == "cbc":
        try:
            text = open(log_path, encoding="utf-8", errors="replace").read()
            if "Stopped on time" in text:
                status = "FEASIBLE" if ok else "NO_SOLUTION"
            mb = re.findall(r"Lower bound:\s+([-\d.e+]+)", text)
            bound = float(mb[-1]) if mb else None
        except OSError:
            pass
    else:
        try:
            info = solver.solverModel.getInfo()
            bound = info.mip_dual_bound
            if info.mip_gap > 1e-6 and status == "OPTIMAL":
                status = "FEASIBLE"
        except Exception:
            pass
    shifts = [c for c, v in zip(built.cands, built.x) if ok and (v.varValue or 0) > 0.5]
    timeline = [(wall, obj, sum(round(u.varValue or 0) for u in built.unders))] if ok else []
    return Result(
        built.backend.name,
        status,
        obj,
        bound,
        _gap(obj, bound),
        wall,
        built.build_seconds,
        len(prob.variables()),
        len(built.cands),
        built.n_constraints,
        timeline,
        shifts,
    )
