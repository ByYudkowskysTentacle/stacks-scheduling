# ADR 0001: Technology stack

- **Status:** Accepted by the maintainer on 2026-10-19 (D43).
- **Date:** proposed 2026-10-01, accepted 2026-10-19
- **Decision log:** `docs/decisions.md` (D1–D43) records the maintainer answers this ADR builds on.

## Context

The hard requirements come from CLAUDE.md §1 and the planning decisions:

- **Self-hosted with `docker compose up`** on one modest server. The pilot runs on an Ubuntu VPS (D8), and development uses Docker Desktop on Windows (D23). A small library IT team must be able to install, back up and upgrade it.
- **Relational and transactional:** PostgreSQL. Bulk changes are all-or-nothing with undo (SPEC §9.3), and every write is audited (CLAUDE.md §6).
- **A real optimization solver** for generate, rebalance and what-if (SPEC §7), meeting the targets for about 30 people over 2 weeks: rebalance under 30 seconds and generate under 5 minutes, on modest hardware. The prototype's PuLP/CBC approach hit time limits on joint two-week problems.
- **Light front end:** server-rendered pages that work on phones and print cleanly, and meet WCAG 2.1 AA (raised to 2.2 AA by D113).
- **Authentication:** local accounts first, then Google Workspace via generic OIDC, then SAML 2.0 (D5), with optional TOTP.
- **Licensing:** AGPL-3.0, confirmed by the maintainer (D3). Every dependency must be compatible.
- **Spring 2027 go-live** (D15, D16), about 14 weeks out, built by a small team. Batteries-included tooling matters more than flexibility.
- **Pure domain core:** domain logic (rules, validation, solver model) lives in a framework-independent module, shared by the web app, the background worker and a CLI (CLAUDE.md §3).

## Options considered

### Application stack

| Option | Summary | Verdict |
|---|---|---|
| **A. Python + Django + HTMX** | Server-rendered pages, CP-SAT in a Python worker, one language throughout. | Strong fit. |
| **A′. A plus TypeScript islands** | As A, with small TS components only where rich interaction earns it (a drag-and-drop schedule grid). | **Chosen** (D4). |
| B. TypeScript/Node + Python solver service | TS web app and API, plus a separate Python service for the solver. | Rejected. OR-Tools has no official JS binding, so the solver and the validation suite (SPEC §8) would live in Python anyway. The TS app becomes a shell around a Python core, the domain rules risk being written twice, and a small IT team has two runtimes and two dependency trees to patch. |
| C. Python JSON API + TypeScript SPA | Django or FastAPI serving only an API, with the whole UI in a SPA. | Rejected for v1. The richest interactivity, but the most work for printing, phones and accessibility, and the slowest route to the spring date. The documented API (SPEC §15) is still built, alongside the server-rendered pages. |

The full comparison, written during planning, covers solver integration, auth, the audit log, background jobs, phones and print, type safety, testing, operations and the contributor pool. Its conclusion: the domain core has to be Python, because that's where CP-SAT runs, and everything else follows from that.

### Solver

Each solver was benchmarked on the same candidate-shift model; details in `spikes/cpsat-benchmark/`:

| Solver | Notes |
|---|---|
| **OR-Tools CP-SAT** | Apache-2.0. Parallel workers, solution hints for warm starts, and a progress callback for showing progress in the UI. |
| CBC via PuLP | EPL-2.0. The prototype's solver. Runs as a command-line subprocess and reports only its final answer. |
| HiGHS via PuLP | MIT. A modern open-source MIP solver. |

See **Benchmark results** below.

## Decision

### Runtime and framework

- **Python 3.13** with **Django**, on the current LTS release when scaffolding begins, moving to each new LTS as it ships.
- **Server-rendered Django templates plus HTMX** for interaction. Plain CSS with a print stylesheet per main view. **TypeScript islands**, built with esbuild and served as static files, only where a view needs them. The schedule grid's drag-and-drop is the expected first case. There's no Node runtime in production.
- **PostgreSQL** (current supported major). Every table that holds organisation data carries `organization_id` (SPEC §1), and location-scoped tables carry `location_id` (D1).

### Domain core and solver

- A **`domain/` package** with no Django or database imports, holding the domain model, the SPEC §8 validation suite, candidate generation and the CP-SAT model. The web app, the worker and a CLI all call it.
- **OR-Tools CP-SAT** for generate, rebalance, what-if and the later engine modes. The settings below come from the benchmark:
  - **Presolve off** (`cp_model_presolve = false`) for this model class. Default presolve spent most of a 10 to 30 second budget simplifying the model before any search on 4 workers, so a rebalance couldn't even return its starting schedule.
  - **Warm-start completion:** fix the existing shifts, solve for the slack and penalty variables (about 1 second), then hint the full assignment. Hinting only the shift choices left CP-SAT unable to use the existing schedule.
  - **Never worse than the start:** a rebalance or targeted move compares its result with the completed warm start, and keeps the existing schedule if the solver didn't beat it within the time limit. The result says so plainly.
  - **Two rebalance options in parallel** (SPEC §7.3.1): the full-fix and limited-change solves run at the same time, each with half the CPUs, under one shared 30-second deadline. This was faster and better than running them one after the other. When the full fix is already within the change limit, the limited solve is skipped.
  - **Workers = CPUs given to the worker container**, 4 on the pilot VPS (D35). The deadline covers model building and warm-start completion, not just the search. The objective uses tiered weights, documented next to the model (CLAUDE.md §4). Every result reports its status and gap; a time-limited result is never presented as optimal.
  - **Keep the model linear** (no CP-only constraint types), so CBC remains a drop-in fallback if CP-SAT ever becomes a problem. The benchmark shows CBC is viable on this formulation.
- **Background solving:** **Procrastinate**, a PostgreSQL-backed task queue, so there's no Redis to run. Solves run in a dedicated `worker` container with its own CPU limit. Progress and results are written to the database and polled by the page via HTMX, so the UI never blocks (SPEC §7.3).

### Auth, audit and email

- **django-allauth** for local accounts, generic OpenID Connect (Google Workspace first, restricted to the campus domain by checking the `hd` claim server-side), SAML 2.0 later, and TOTP through allauth's MFA module. Tests use a local Keycloak container, never Google (D5).
- **django-pghistory** for the audit log. It is trigger-based, so bulk operations and raw SQL are captured too, and the "who" comes from request context. It records before and after values for schedules, caps, wages and tags (CLAUDE.md §6).
- Authorization is enforced in one place, a permission layer called by every view and API endpoint. Roles are student, shift lead (D21, D25), supervisor and admin.
- **Email** through Django's SMTP backend, configured from the environment (D14), plus an in-app notification table. Sending happens only inside the publish transaction's on-commit hook (CLAUDE.md §2.2).

### Deployment

`docker compose` with four services and named volumes:

| Service | Role |
|---|---|
| `caddy` | TLS with automatic certificates, the only service exposing ports (80 and 443). |
| `web` | Django under Gunicorn. |
| `worker` | Procrastinate worker running the solver and imports, with CPU and memory limits. |
| `db` | PostgreSQL, not published to the host. |

- **Backups:** a documented `pg_dump` job (a compose profile or host cron) to an off-box encrypted target, with a tested restore procedure in the install guide.
- **Upgrades:** `docker compose pull && docker compose up -d`. Migrations run in a one-shot `migrate` step before `web` starts.
- **Configuration and secrets** come from `.env` and Docker secrets files, never from the database in plain text (CLAUDE.md §6).

### Development and testing

- **pytest** with pytest-django. **Hypothesis** for engine property tests (CLAUDE.md §5). **Playwright** for print and visual snapshots of the availability grid and hours report. Adapter contract tests against fakes and the synthetic files in `fixtures/`.
- **ruff** for linting and formatting, **pyright** for type checking (strict in `domain/`).
- **GitHub Actions** on the private repository (D22): tests, the solver benchmark (results kept as artefacts and compared with the last run), and image builds.

### License

**AGPL-3.0-or-later** (D3, confirmed in D43). The full text is in `LICENSE`, and every source file carries the SPDX identifier `AGPL-3.0-or-later`. All planned runtime dependencies are compatible:

| License | Dependencies |
|---|---|
| BSD | Django, django-pghistory, django-pgtrigger, oauthlib, idna |
| Apache-2.0 | OR-Tools, Caddy, requests |
| MIT | django-allauth, Procrastinate, Gunicorn, WhiteNoise, python3-saml, xmlsec, PyJWT, urllib3, charset-normalizer |
| LGPL-3.0 | psycopg |
| PostgreSQL License | PostgreSQL |
| Permissive (BSD family) | HTMX |
| MPL-2.0 | certifi (CA certificates, used unmodified) |

Dev-only tools (Hypothesis, MPL-2.0; Playwright, Apache-2.0) don't affect distribution. Each dependency's license gets rechecked when it's added.

## Benchmark results

Full tables: `spikes/cpsat-benchmark/results/SUMMARY.md`. Method: `spikes/cpsat-benchmark/README.md`.

**Setup.** Three seeded synthetic rosters of the pilot's shape (28 students, the §3.1 hours and Chapel closure, the §12 opener, leads, new employees, a `not_entered` student and soft tag rules). Each has about 17,000 candidate shifts and 20,000 variables. Scenarios: generate (no rotation and strict rotation, 5-minute limit) and three rebalances after a rule change (add a priority 20-hour target, relax strict rotation, cut the busiest person's cap; 30-second limit). Every result was rechecked by an independent checker. Runs used Docker with CPUs capped at 2, 3 or 4, on a 16-core workstation with several containers running at once.

**Findings.**

| Question | Result |
|---|---|
| Does CP-SAT meet the targets on 4 CPUs? | **Yes, with presolve off.** Every run had zero coverage shortfall and zero hard-rule violations. Generate reached the minimum over-coverage (2 to 3 hours) on all seeds, proven optimal in 50 to 156 s or within 0.04 slots of optimal at 5 minutes. Rebalances found a first solution in under 1 s from the warm start. Cap rebalances were proven optimal in 7 to 19 s; rotation rebalances were optimal on 2 of 3 seeds (the third within 0.2 slots); target rebalances ended 0.5 to 2.9 slots from optimal. |
| What did default presolve do? | In the logged run, presolve took about 12 s before any search. Rebalances got no first solution for 6 to 9 s, ended up to 12.6 slots from optimal at 30 s, and met the 20-hour target on 0 of 3 seeds (presolve off: 2 of 3). In an early short run, default presolve returned 42 to 81 hours of over-coverage. |
| Is 2 vCPU enough? | Mostly. Generate quality matched 4 CPUs. Rebalances were slightly weaker (target met on 1 of 3 seeds, worst case 1 hour short at 30 s). The pilot plans for 4 vCPU (D35). |
| Does the web app and database competing for CPU matter? | 3 workers on 3 CPUs matched 4 CPUs on every scenario, so leaving a core for `web` and `db` costs little. |
| Can both rebalance options fit in 30 s? | **Yes, in parallel.** Two solves at the same time on 2 workers each finished in 13.6 to 31.4 s wall time in total, and were proven optimal in 11 of 12 solves. One after the other (15 s each on 4 workers), 5 of 12 solves ended short of optimal. At the 30% default limit, all six full fixes moved 27.5 to 80 of about 295 hours, so only one option would have been shown. At a 10% limit the limited option left real issues (up to 25.5 hours of over-coverage, or the target up to 10.5 hours short), which is the case the "explain and suggest" screen (D36) is for. |
| How does CBC compare? | **Better than the prototype suggested.** On this candidate formulation CBC matched CP-SAT's final quality on all 3 seeds: zero shortfall, every target met, strict generate optimal in 30 to 134 s. But through PuLP it reports nothing until it finishes, so there's no progress to show, no anytime answer, and no usable warm-start signal. The prototype's time-limit trouble was likely the formulation, not only the solver. |
| How does HiGHS compare? | **Not reliable here.** It overran its time limits (up to 48 s on a 30 s budget), and one rebalance came back with 21.5 hours of coverage shortfall. |

**Caveats.** The runs shared one workstation, so absolute times are indicative. Re-run the same Docker benchmark on the actual VPS once it's provisioned, and keep it in CI to catch regressions as duties, tag rules and desired hours are added to the real engine. The spike counts a retimed shift fully as "moved"; the real engine credits retimes (D33), so real churn will be lower than reported here.

**Solver decision: CP-SAT,** for its parallel search, sub-second warm starts, live progress, and an anytime best answer that supports the never-worse rule and the two-option design. Keep the model linear so CBC remains a fallback.

## Consequences

- **One language and one dependency tree** to patch, and a small compose stack, which suits a small library IT team.
- **Django's batteries** (ORM and migrations, admin for supervisor configuration, forms, CSRF, sessions) shorten the path to the spring date.
- **The engine gets the best-supported CP-SAT binding,** sharing types with the validation suite and the CLI. Property tests sit next to the model.
- **The hardest UI piece,** the interactive schedule grid, may need a TypeScript island and a small esbuild step in CI. Everything else stays HTMX.
- **Solver settings are tied to this model's shape.** Presolve off, the warm-start completion and the parallel two-option solve are benchmark findings, not universal truths. The CI benchmark guards against regressions as the model grows (duties, tag rules and desired hours in milestone 4, D20, D32).
- **Rebalances can't always meet every target in 30 seconds,** and sometimes no schedule can. The engine must say which, using the gap and the explanation: "best found, not proven" versus "proven impossible from this starting point".
- **Hosting and FERPA:** the VPS is internet-facing and may need a campus data-processing agreement (D8). The compose stack keeps the database private and TLS on by default.

## Follow-ups

1. When the VPS is provisioned, re-run the benchmark on it to confirm the timings.
