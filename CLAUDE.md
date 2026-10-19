# CLAUDE.md

Guidance for Claude Code working in this repository.

This project is an open-source, self-hosted employee scheduling and management system for academic libraries that run on student workers. It covers what Homebase does for us today (availability, shift scheduling, drafts and publishing, hours tracking) and adds the things libraries need that Homebase does not handle well: skill-based coverage rules, per-student hour caps and funding limits, semester class-schedule availability, and payroll export to Paycom.

**Read `docs/SPEC.md` before writing any code.** It holds the domain model, the scheduling rules, and the acceptance scenarios. This file covers how to work.

---

## 1. First session: decide the stack, then record it

The stack is intentionally not chosen yet. In the first planning session:

1. Read `docs/SPEC.md` in full.
2. Propose a stack against these hard requirements and write it up as `docs/adr/0001-stack.md` (context, options considered, decision, consequences):
   - Runs self-hosted on one modest server or VM with `docker compose up`. A small library IT department must be able to install, back up and upgrade it without specialist help.
   - Relational database (PostgreSQL is the expected default). All scheduling state is transactional.
   - A real optimization solver for schedule generation (see SPEC §7). Evaluate OR-Tools CP-SAT first. Our prototype used PuLP/CBC and hit time limits on joint two-week problems, so benchmark before committing.
   - Server-rendered or lightweight front end that works well on phones (students check shifts on phones) and prints cleanly (supervisors print the availability grid).
   - Campus SSO (SAML 2.0 and OIDC) plus local accounts for small sites.
   - An open-source license compatible with the chosen dependencies. Recommend one in the ADR and flag it for the maintainer to confirm.
3. Stop and get the maintainer's approval on the ADR before scaffolding.

After the stack is approved, fill in section 8 of this file (commands) and keep it current.

## 2. Non-negotiable principles

1. **Student data is sensitive.** Class schedules, pay rates, work-study awards and hours are student records. Collect the minimum, never log personal data, never commit real student data, and keep all fixtures synthetic. See SPEC §11.
2. **Drafts before publishing.** Every schedule change is a draft until a supervisor publishes it. Notifications go out only on publish. Nothing in this system should message a student as a side effect of editing.
3. **No silent destructive actions.** Deleting shifts, archiving employees or overwriting a published schedule needs an explicit confirmation step and leaves an audit record. Prefer reassigning or retiming an existing shift over delete-and-recreate, so history and IDs survive.
4. **Every change is verified.** After any schedule mutation (manual edit, solver run, bulk import), run the full validation suite from SPEC §8 and show the result: coverage gaps, over-coverage, availability conflicts, cap breaches, and missing required roles. A change that passes quietly still reports "0 issues".
5. **Explain, don't just decide.** When the solver or a suggestion engine proposes changes, show who gains and who loses hours, why someone ended up with few hours (limited availability or balancing), and which rule forced a trade-off.
6. **Minimal churn.** Re-optimizing an existing schedule should keep as many current shifts as possible. Supervisors ask for small moves ("give Joshua a couple of Palmer's hours"), and the system should honour that scale.
7. **Accessible and printable.** Aim for WCAG 2.1 AA. Every main view needs a usable print stylesheet (landscape, repeated table headers, no clipped cells). Don't rely on colour alone to show state.

## 3. How to work

- Work in small, reviewable slices that match the milestones in SPEC §13. Each slice ends with passing tests and a short note on what changed.
- For anything touching the scheduling engine, write the invariant tests first (SPEC §8 and §12). The engine is the heart of the product, and the most expensive place to get wrong.
- When a rule in the spec is ambiguous, don't guess silently. Choose the conservative interpretation, note it in `docs/decisions.md`, and raise it in the summary.
- Keep domain logic (rules, validation, solver model) in a pure, framework-independent module with no web or database imports, so it can be unit-tested and reused by the CLI.
- Keep integration adapters (Paycom, calendar/ICS, CSV import) behind interfaces with a fake implementation for tests. Never call a real external system from tests.
- Use the domain terms from SPEC §2 exactly in code, UI and docs (for example `Availability`, `AvailabilityBlock`, `Shift`, `CoverageTarget`, `HourCap`, `Tag`). Don't invent synonyms.

## 4. Scheduling engine rules of thumb

These came from building real schedules for a library of about 30 student workers:

- Time is discretised into 15-minute slots. Shift starts and ends land on slot boundaries.
- Model shifts as candidate intervals per person and day, filtered by availability, operating hours and closures, and choose among them. This worked far better than free-form start and end variables.
- Treat hard rules as constraints and preferences as weighted objective terms. Coverage shortfall should be much more expensive than anything else, so that no preference can buy a gap.
- Where one rule clearly outranks another, use lexicographic or tiered weights rather than one blended score, and document the weights in code next to the model.
- Always support warm-starting from the current schedule with a "keep existing shift" bonus. This is what makes small edits produce small diffs.
- Offer a what-if mode that solves without saving and returns a diff plus per-person hour changes. Supervisors used this constantly before committing.
- Enforce solver time limits and report the optimality gap. Never present a time-limited result as optimal.
- Some constraints make exact coverage impossible. For example, an opener limited to 90 minutes before class, next to a closure, forces one slot of over-coverage if every shift must be at least 2 hours. Detect this, pick the least-bad option, and tell the user plainly.

## 5. Testing expectations

- Unit tests for every validation rule, one failing and one passing case each.
- Property-based tests for the engine: for random synthetic rosters, every returned schedule satisfies all hard constraints or reports exactly which ones it could not.
- The acceptance scenarios in SPEC §12 are the regression suite. Add a scenario whenever a bug is found in scheduling behaviour.
- Snapshot or visual tests for the printable availability grid and the hours report.
- Adapter contract tests for the Paycom export, run against the fake and against the sample file formats in `fixtures/paycom/`.

## 6. Security and privacy checklist (every PR)

- Enable the commit guard once per clone: `git config core.hooksPath .githooks`. It runs gitleaks and `tools/check_sensitive.py` on staged changes; GitHub Actions runs both on every push. Never bypass it with `--no-verify`; fix the finding or, for a genuine false positive, allowlist it with a reason.
- No personal data in logs, error messages, analytics or fixtures.
- Authorization is checked server-side on every endpoint. Students see their own data plus the published schedule. Supervisors see their department. Admins see everything.
- Secrets (SSO, Paycom credentials) come from environment or secret files and are never stored in the database in plain text.
- Every write to schedules, caps, wages or tags produces an audit-log entry (who, when, before, after).
- Data export and deletion are available for a single student record, to support records requests and offboarding.

## 7. Things to avoid

- Don't build a general-purpose HR suite. Scope is scheduling, availability, hours, time tracking and payroll export for student-worker-heavy libraries.
- Don't hard-code any one library's hours, closures, roles or caps. Everything in SPEC §3–§6 is configuration.
- Don't assume blank availability means available without flagging it. In practice, "nothing entered" usually means "hasn't filled it in yet".
- Don't auto-publish, auto-notify or auto-approve anything.
- Don't depend on undocumented behaviour of third-party systems (Homebase, Paycom). Integrations go through documented APIs or agreed file formats only.

## 8. Commands

Stack approved in `docs/adr/0001-stack.md` (D43). Development runs in Docker Desktop (D23), so commands assume Docker; the `uv` commands also work on a machine with Python 3.13, [uv](https://docs.astral.sh/uv/) and a local PostgreSQL.

```bash
# once per clone: enable the secrets and personal-data commit guard
git config core.hooksPath .githooks
python tools/check_sensitive.py --all   # same personal-data check CI runs

# development stack: Django dev server on http://localhost:8000, Postgres on 127.0.0.1:5432
cp .env.example .env                     # then set POSTGRES_PASSWORD and DJANGO_SECRET_KEY
docker compose -f compose.yaml -f compose.dev.yaml up --build
docker compose -f compose.yaml -f compose.dev.yaml exec web python manage.py seed_demo   # synthetic demo data

# without Docker for the app (needs Postgres; settings come from the environment)
uv sync
DJANGO_DEBUG=1 uv run python manage.py migrate
DJANGO_DEBUG=1 uv run python manage.py seed_demo          # supervisor@example.edu / demo-password-123
DJANGO_DEBUG=1 uv run python manage.py runserver

# checks CI runs
uv run ruff format --check . && uv run ruff check .
uv run pyright                           # strict, domain/ only (D53)
uv run pytest -q                         # uses app.config.test_settings and Postgres
uv run python manage.py makemigrations --check --dry-run --settings=app.config.test_settings

# production (see docs/install.md once written)
docker compose up -d --build
docker compose run --rm web python manage.py bootstrap --organization "Your Library" --admin-email you@example.edu

# solver benchmark spike (spikes/cpsat-benchmark/README.md has details)
docker build -t stacks-bench spikes/cpsat-benchmark
docker run --rm --cpus=4 -v "$PWD/spikes/cpsat-benchmark/results:/bench/results" stacks-bench --solver cpsat --seeds 1 2 3 --workers 4 --out runs.jsonl
python spikes/cpsat-benchmark/summarize.py   # writes results/SUMMARY.md
```

On Windows Git Bash, prefix `docker run` with `MSYS_NO_PATHCONV=1` so the volume path isn't rewritten.

## 9. Where things live

```
docs/SPEC.md          product and domain spec (source of truth for behaviour)
docs/adr/             architecture decision records
docs/decisions.md     running log of spec interpretations and open questions
domain/               pure domain model, validation rules, solver model (no Django imports)
adapters/             Paycom, ICS/calendar, CSV import/export
app/config/           Django settings (from environment), URLs, WSGI; test_settings for pytest
app/accounts/         sign-in accounts (email login, allauth, TOTP)
app/core/             organization, locations, hours, closures, terms, roles, the permission
                      layer (permissions.py), audit log helpers, management commands
app/people/           employees, pay roles and wages, tags, invitations
app/availability/     term availability, entry form and paint grid, the availability grid
app/imports/          roster and availability CSV imports: preview, apply, undo
app/static/js/        small progressive-enhancement scripts (no build step)
app/templates/        server-rendered pages; app/static/ holds CSS (screen and print)
deploy/               Caddyfile; compose.yaml and compose.dev.yaml sit at the root
fixtures/             synthetic rosters (fixtures/synthetic.py), sample import/export files
tests/domain/         unit and property tests for domain/
tests/adapters/       parser and adapter contract tests
tests/app/            view, permission and integration tests against PostgreSQL
tests/browser/        Playwright tests: paint grid, print layout (skipped without Chromium;
                      set CHROMIUM_PATH to use a local binary)
docs/formats/         the documented import formats (D13)
```
