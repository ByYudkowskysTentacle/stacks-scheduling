"""Can a rebalance produce both options (SPEC §7.3.1) inside 30 seconds on 4 vCPUs?

For each seed and rule change, builds a starting schedule, then solves the full-fix and
limited-change options in two ways:
  seq: one after the other, 15 seconds each on 4 workers
  par: at the same time, 30 seconds each on 2 workers
plus the full fix alone for 60 seconds on 4 workers, to see whether time is what's missing.
Appends JSON lines to results/two-options.jsonl.

    python two_options.py --seeds 1 2 3 --limit-pct 10
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time

from bench import PRESOLVE, RESULTS, protected_minimum
from check import check
from model import W_KEEP_REBALANCE, CpSatBackend, build_candidates, build_model, solve_cpsat
from synth import build_roster

CHANGES = {
    "rb_target": ("none", lambda r: (setattr(r.get("S04"), "target_week_hours", 20.0),
                                     setattr(r.get("S04"), "target_priority", 3))),
    "rb_rotation": ("strict", lambda r: setattr(r.rules, "rotation", "preferred")),
}


def solve(roster, prev, limit, workers, max_moved=None):
    cands = build_candidates(roster)
    existing = {s.key for s in prev}
    built = build_model(roster, CpSatBackend(), cands, existing=existing, keep_weight=W_KEEP_REBALANCE,
                        protect=protected_minimum(roster, prev), max_moved_slots=max_moved)
    res = solve_cpsat(built, limit, workers, params=PRESOLVE["off"])
    moved = sum(k[3] - k[2] for k in existing - {s.key for s in res.shifts}) / 4
    c = check(roster, res.shifts)
    return {
        "status": res.status, "wall": round(res.wall_seconds, 1),
        "gap": None if res.gap is None else round(res.gap, 2),
        "moved_h": moved, "short_h": c["shortfall_hours"], "over_h": c["over_hours"],
        "tag_miss_h": c["lead_miss_hours"] + c["cataloguer_miss_hours"] + c["new_alone_hours"]
        + sum(c["processor_short_hours"]),
        "target_short": c["target_short_hours"], "hard": len(c["hard_violations"]),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    p.add_argument("--limit-pct", type=float, default=10.0)
    p.add_argument("--gen-limit", type=float, default=120)
    args = p.parse_args()
    out = os.path.join(RESULTS, "two-options.jsonl")

    for seed in args.seeds:
        base = build_roster(seed)
        starts = {}
        for rotation in ("none", "strict"):
            r = base.clone()
            r.rules.rotation = rotation
            cands = build_candidates(r)
            starts[rotation] = solve_cpsat(build_model(r, CpSatBackend(), cands), args.gen_limit, 4,
                                           params=PRESOLVE["off"]).shifts
        for name, (rotation, change) in CHANGES.items():
            prev = starts[rotation]
            roster = base.clone()
            roster.rules.rotation = rotation
            change(roster)
            scheduled = sum(s.length for s in prev)
            max_moved = int(scheduled * args.limit_pct / 100)
            row = {"seed": seed, "scenario": name, "limit_pct": args.limit_pct,
                   "scheduled_h": scheduled / 4, "limit_h": max_moved / 4}

            row["full_60s"] = solve(roster, prev, 60, 4)
            row["seq_full"] = solve(roster, prev, 15, 4)
            row["seq_limited"] = solve(roster, prev, 15, 4, max_moved)

            par = {}
            t0 = time.perf_counter()
            threads = [
                threading.Thread(target=lambda: par.__setitem__("full", solve(roster, prev, 30, 2))),
                threading.Thread(target=lambda: par.__setitem__("limited", solve(roster, prev, 30, 2, max_moved))),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            row["par_wall"] = round(time.perf_counter() - t0, 1)
            row["par_full"], row["par_limited"] = par["full"], par["limited"]

            with open(out, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
            for k in ("full_60s", "seq_full", "seq_limited", "par_full", "par_limited"):
                v = row[k]
                print(f"seed={seed} {name:11} {k:11} {v['status']:9} wall={v['wall']:5}s gap={v['gap']} "
                      f"moved={v['moved_h']}h/{row['limit_h']}h over={v['over_h']}h tag_miss={v['tag_miss_h']}h "
                      f"target_short={v['target_short']} hard={v['hard']}", flush=True)
            print(f"seed={seed} {name:11} par_wall={row['par_wall']}s", flush=True)


if __name__ == "__main__":
    main()
