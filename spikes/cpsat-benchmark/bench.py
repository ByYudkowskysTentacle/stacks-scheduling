"""Solver benchmark for the stack ADR (docs/adr/0001-stack.md).

Runs generate and rebalance scenarios on synthetic rosters and appends one JSON line per
run to results/runs.jsonl. Targets from SPEC §7.3 for about 30 people over 2 weeks:
rebalance under 30 seconds, full generation under 5 minutes.

    python bench.py --solver cpsat --seeds 1 2 3 --workers 2 4
    python bench.py --solver cbc --seeds 1 --workers 4
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import time

from check import check
from model import W_KEEP_REBALANCE, CpSatBackend, PulpBackend, build_candidates, build_model, solve_cpsat, solve_pulp
from synth import build_roster, week_of

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results")


PRESOLVE = {"default": {}, "iter1": {"max_presolve_iterations": 1}, "off": {"cp_model_presolve": False}}


def run_one(roster, solver, limit, workers, existing=None, protect=None, start_step=2, seed=0, log_tag="run",
            presolve="default"):
    cands = build_candidates(roster, start_step=start_step)
    backend = CpSatBackend() if solver == "cpsat" else PulpBackend(solver)
    keep = W_KEEP_REBALANCE if existing is not None else 0
    built = build_model(roster, backend, cands, existing=existing, keep_weight=keep, protect=protect)
    if solver == "cpsat":
        return solve_cpsat(built, limit, workers, seed, PRESOLVE[presolve])
    return solve_pulp(built, limit, workers, os.path.join(RESULTS, f"{log_tag}.log"))


def milestones(res):
    tl = res.timeline
    if not tl:
        return {}
    final = res.objective
    tol = max(0.01 * abs(final), 1.0)
    return {
        "t_first": round(tl[0][0], 2),
        "t_zero_shortfall": next((round(t, 2) for t, _, sf in tl if sf == 0), None),
        "t_within_1pct": next((round(t, 2) for t, o, _ in tl if o - final <= tol), None),
    }


def protected_minimum(roster, shifts):
    """Protect people whose weekly hours are below the median of those working (SPEC §6.4)."""
    weekly = {}
    for s in shifts:
        k = (s.emp, week_of(s.day))
        weekly[k] = weekly.get(k, 0) + s.length
    if not weekly:
        return {}
    median = statistics.median(weekly.values())
    return {k: v for k, v in weekly.items() if v < median}


OUT = "runs.jsonl"


def record(**fields):
    with open(os.path.join(RESULTS, OUT), "a", encoding="utf-8") as f:
        f.write(json.dumps(fields) + "\n")
    print(
        f"{fields['solver']:5} seed={fields['seed']} w={fields['workers']} {fields['scenario']:11} "
        f"{fields['status']:9} wall={fields['wall_seconds']:7.1f}s gap={fields['gap']} "
        f"short={fields['check']['shortfall_hours']}h over={fields['check']['over_hours']}h "
        f"kept={fields.get('kept_pct')} hard={len(fields['check']['hard_violations'])} "
        f"first={fields.get('t_first')}s target_short={fields['check']['target_short_hours']}",
        flush=True,
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--solver", choices=["cpsat", "cbc", "highs"], default="cpsat")
    p.add_argument("--seeds", type=int, nargs="+", default=[1])
    p.add_argument("--workers", type=int, nargs="+", default=[4])
    p.add_argument("--gen-limit", type=float, default=300)
    p.add_argument("--rebal-limit", type=float, default=30)
    p.add_argument("--start-step", type=int, default=2, help="candidate start step in 15-minute slots")
    p.add_argument("--scenarios", nargs="+", default=["gen_none", "gen_strict", "rb_target", "rb_rotation", "rb_cap"])
    p.add_argument("--presolve", choices=sorted(PRESOLVE), default="off", help="CP-SAT presolve setting")
    p.add_argument("--label", default="")
    p.add_argument("--out", default="runs.jsonl", help="results file name under results/")
    args = p.parse_args()
    global OUT
    OUT = args.out
    os.makedirs(RESULTS, exist_ok=True)
    env = {"machine": platform.processor(), "cpus": os.cpu_count(), "python": platform.python_version()}

    for seed in args.seeds:
        for workers in args.workers:
            base = build_roster(seed)
            common = dict(solver=args.solver, seed=seed, workers=workers, start_step=args.start_step,
                          presolve=args.presolve,
                          label=args.label, env=env, when=time.strftime("%Y-%m-%dT%H:%M:%S"))
            starts = {}

            def emit(name, roster, res, existing=None):
                c = check(roster, res.shifts)
                c.pop("weekly_hours")
                kept = None
                if existing is not None:
                    kept = round(100 * len(existing & {s.key for s in res.shifts}) / max(1, len(existing)), 1)
                record(scenario=name, status=res.status, objective=res.objective, bound=res.bound,
                       gap=None if res.gap is None else round(res.gap, 5), wall_seconds=round(res.wall_seconds, 2),
                       build_seconds=round(res.build_seconds, 2), n_candidates=res.n_candidates, n_vars=res.n_vars,
                       n_constraints=res.n_constraints, kept_pct=kept,
                       n_existing=None if existing is None else len(existing), check=c,
                       **milestones(res), **common)

            needs = {"gen_none": {"gen_none", "rb_target", "rb_cap"}, "gen_strict": {"gen_strict", "rb_rotation"}}
            for name, rotation in (("gen_none", "none"), ("gen_strict", "strict")):
                if not needs[name] & set(args.scenarios):
                    continue
                roster = base.clone()
                roster.rules.rotation = rotation
                res = run_one(roster, args.solver, args.gen_limit, workers, start_step=args.start_step,
                              seed=seed, log_tag=f"{args.solver}_{seed}_{workers}_{name}", presolve=args.presolve)
                starts[rotation] = res.shifts
                if name in args.scenarios:
                    emit(name, roster, res)

            rebalances = {
                # Scenario 6-style: a priority hour target is added after the schedule exists.
                "rb_target": ("none", lambda r, shifts: setattr(r.get("S04"), "target_week_hours", 20.0)
                              or setattr(r.get("S04"), "target_priority", 3)),
                # Scenario 12: strict rotation relaxed to preferred.
                "rb_rotation": ("strict", lambda r, shifts: setattr(r.rules, "rotation", "preferred")),
                # The busiest general person's weekly cap drops to 4 hours below their peak week, so it binds.
                "rb_cap": ("none", lambda r, shifts: setattr(r.get(busiest(r, shifts)[0]), "weekly_cap",
                                                             max(2.0, busiest(r, shifts)[1] - 4.0))),
            }
            for name, (rotation, change) in rebalances.items():
                if name not in args.scenarios or not starts.get(rotation):
                    continue
                prev = starts[rotation]
                roster = base.clone()
                roster.rules.rotation = rotation
                change(roster, prev)
                existing = {s.key for s in prev}
                res = run_one(roster, args.solver, args.rebal_limit, workers, existing=existing,
                              protect=protected_minimum(roster, prev), start_step=args.start_step, seed=seed,
                              log_tag=f"{args.solver}_{seed}_{workers}_{name}", presolve=args.presolve)
                emit(name, roster, res, existing)


def busiest(roster, shifts):
    """(employee, peak weekly hours) for the general staff member with the highest week."""
    special = {roster.duties.opener, *dict(roster.duties.leads)}
    weekly = {}
    for s in shifts:
        if s.emp not in special:
            k = (s.emp, week_of(s.day))
            weekly[k] = weekly.get(k, 0) + s.length
    (emp, _week), peak = max(weekly.items(), key=lambda kv: (kv[1], kv[0]))
    return emp, peak / 4


if __name__ == "__main__":
    main()
