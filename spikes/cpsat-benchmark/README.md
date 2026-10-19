# Solver benchmark spike

Throwaway code that answers one question for `docs/adr/0001-stack.md`: can OR-Tools CP-SAT meet the SPEC §7.3 performance targets on a pilot-sized problem, on modest hardware, and how does it compare with the CBC (PuLP) approach the prototype used?

This is **not** the production engine. The real engine will live in `domain/` with invariant tests written first (CLAUDE.md §3).

## What it models

- **Roster** (`synth.py`): seeded, fully synthetic. 28 regular students and 4 `non_regular` employees, one location, the SPEC §3.1 operating hours with the Wednesday Chapel closure (73.5 open hours a week), and a two-week period starting Saturday. It includes the §12 special cases: an opener with a 1.5-hour duty exception on Mon, Wed and Fri; two leads on different weeks; four new employees with a 12-hour period cap; one `not_entered` student; and a student with a 10-hour weekly cap.
- **Model** (`model.py`): candidate shifts per person and day (30-minute starts, lengths of 2 to 4 hours in 30-minute steps), exact coverage of 2, shape rules, caps, duties, soft tag rules, hour targets, rotation, fairness, and keep-existing bonuses. The objective uses tiered weights, documented at the top of the file. The same model is emitted to CP-SAT directly, or to CBC and HiGHS through PuLP.
- **Checker** (`check.py`): recomputes the hard rules and the coverage and tag metrics from the returned shifts alone, so a modelling bug can't pass quietly.
- **Scenarios** (`bench.py`):
  - `gen_none`: generate from scratch, no rotation.
  - `gen_strict`: generate from scratch, strict A/B rotation.
  - `rb_target`: rebalance after adding a priority 20-hour target (scenario 6).
  - `rb_rotation`: rebalance after relaxing the rotation from strict to preferred (scenario 12).
  - `rb_cap`: rebalance after the busiest person's weekly cap drops to 4 hours below their peak week.
- **Two options** (`two_options.py`): solves the full-fix and limited-change rebalance options (SPEC §7.3.1) one after the other and in parallel, to check both fit in 30 seconds.

## Running

The timed runs use Docker, with CPUs capped to stand in for a small VPS:

```bash
docker build -t stacks-bench spikes/cpsat-benchmark
docker run --rm --cpus=2 -v "$PWD/spikes/cpsat-benchmark/results:/bench/results" stacks-bench --solver cpsat --seeds 1 2 3 --workers 2
```

Results are appended to `results/<--out>.jsonl`, one JSON line per run. `python summarize.py` writes `results/SUMMARY.md`. The `smoke*` files are early runs from before the warm-start and presolve fixes, kept for the record and excluded from the summary.

```bash
docker run --rm --cpus=4 -v "$PWD/spikes/cpsat-benchmark/results:/bench/results" --entrypoint python stacks-bench two_options.py --seeds 1 2 3 --limit-pct 10
```

## Results

See `results/SUMMARY.md`.
