# Decisions and spec interpretations

Running log of maintainer answers, spec interpretations and open questions. Newest last.

## 2026-09-28 – Planning Q&A, round 1

| # | Question (SPEC ref) | Decision |
|---|---|---|
| D1 | Locations in v1 (§16.5) | One location first. Keep `location_id` and `organization_id` on every relevant table so multi-location stays possible; no cross-location UI in v1. |
| D2 | Clock in and out (§16.2, §10.1) | Students punch in Paycom today. v1 has no clock in/out. Paycom remains the system of record for punches. Long-term goal: this system sends punches to Paycom, but there is no API access to test against yet. |
| D3 | License (§16.1) | AGPL-3.0, confirmed by the maintainer. |

### Interpretations

- **D2 consequence.** Milestone 7 shrinks to Paycom export and funding tracking. Keep a `Timecard` concept and a punch-export interface (with a fake adapter) in the design so a Paycom punch integration can be added later against documented API access only (CLAUDE.md §7). How actual hours get back into the system for funding burn-down is open (see round 2).

## 2026-09-28 – Planning Q&A, round 2

| # | Question (SPEC ref) | Decision |
|---|---|---|
| D4 | Stack (CLAUDE.md §1) | Option A′: Python + Django + HTMX (server-rendered) + PostgreSQL + OR-Tools CP-SAT in a background worker, with a pure `domain/` package. TypeScript islands only where rich interaction needs them (for example a drag-and-drop schedule grid). To be written up in `docs/adr/0001-stack.md` after a CP-SAT benchmark spike. |
| D5 | SSO provider (§16.6) | The pilot campus uses Google Workspace. The maintainer wants a path to generic OIDC in future. |
| D6 | Co-worker visibility (§16.7) | Students see the full published schedule with co-workers' preferred names on shared shifts. Never co-workers' availability, hours, wages, tags or notes. |
| D7 | Actual hours for funding (§10.3) | Supervisor imports a Paycom hours/timecard report (CSV) per pay period, with configurable column mapping built from the real report format. Future: an in-app clock-in system used to reconcile against Paycom. |

### Interpretations

- **D5.** Google Workspace sign-in is OIDC, so the first SSO integration is Google via OIDC, restricted to the campus Workspace domain (`hd` claim checked server-side, not just requested). It is built on a generic OIDC provider configuration, so any other OIDC IdP (Entra, Okta, Keycloak) is a config change rather than code. SAML 2.0 stays in scope per SPEC §11 but ships after OIDC. Local accounts remain first (milestone 1). Tests run against a local Keycloak container, never against Google.
- **D6.** "Names" means preferred name only. The published-schedule endpoint returns a restricted shift projection for students (time, location, pay-role colour, co-worker preferred name) and is authorised server-side.
- **D7.** Imported actuals are the only source of "actual spend". Until a report is imported for a period, funding views show projected spend and label it as projection. The Paycom report importer sits behind an adapter interface with a fake, tested against sample files in `fixtures/paycom/` built from the agreed format (synthetic data only).

## 2026-09-28 – Planning Q&A, round 3

| # | Question (SPEC ref) | Decision |
|---|---|---|
| D8 | Hosting | Pilot runs with Docker Compose on an Ubuntu VPS. |
| D9 | Paycom files (§10.2, §16.3) | Maintainer can export reports from Paycom. Whether hours *import* into Paycom is available is unknown; to be asked of the Paycom administrator. |
| D10 | FWS award source (§10.3, §16.4) | Financial aid provides a CSV each term; imported with a configurable column mapping. |
| D11 | Homebase migration | Import roster and availability. |

### Interpretations

- **D8.** A VPS is internet-facing and usually run by a third party, so: TLS termination in the compose stack (Caddy with automatic certificates), only 80/443 exposed, Postgres not published, off-box encrypted backups documented in the install guide. Flag for the campus: hosting student records with a VPS provider may need a data-processing agreement or registrar/counsel sign-off (SPEC §11). The install guide stays generic Linux + Docker so an on-campus VM works the same way.
- **D9.** The actuals importer (D7) is unblocked once the maintainer shares a Paycom report's header row and a few rows with synthetic values. The Paycom hours export (§10.2) stays blocked until the Paycom admin confirms an import path and provides the template.
- **D10.** Real financial aid files are never committed. The fixture is a synthetic CSV matching the real column layout. Import is a bulk operation (SPEC §9.3): one transaction, one audit entry, undo.
- **D11.** Only through a documented Homebase export or API (CLAUDE.md §7); see round 4 for the source. Imported availability gets status `needs_review` until the student or supervisor confirms it, since it may be from a previous term.

## 2026-09-28 – Planning Q&A, round 4

| # | Question (SPEC ref) | Decision |
|---|---|---|
| D12 | Paycom direction (§10.1, §10.2) | Primary Paycom goal is **punch relay**: students clock in and out in this app, and the app sends each punch to Paycom so they only clock in on one platform. |
| D13 | Homebase migration source | Our own documented roster CSV and availability CSV formats, tool-agnostic. The supervisor fills them from Homebase. No Homebase-specific adapter. |
| D14 | Notifications (§9.1) | Email via SMTP (settings from environment) plus an in-app notification list. SMS and push later. |
| D15 | Go-live | Spring 2027 term (around January 2027). |

### Interpretations

- **D12 changes the spec.** SPEC §10.2 treats a Paycom API as optional; for this maintainer it is the headline integration. It depends entirely on documented Paycom API access (punch endpoint, auth, sandbox), which the library does not have yet (CLAUDE.md §7: no undocumented behaviour). Design consequences:
  - Punches are recorded locally first, then relayed through a `PunchRelay` interface (Paycom adapter plus a fake for tests) using an outbox: idempotent, retried, with per-punch sync status visible to the student and supervisor.
  - A relay failure must never silently leave a student unclocked in Paycom, because that affects pay. The failure behaviour is a maintainer decision (round 5).
  - Paycom stays the payroll system of record. The report import (D7) reconciles app punches against Paycom's timecards.
  - The file-based hours export (§10.2) is deprioritised behind the relay.
- **D12 lead time.** Getting Paycom API access is likely the longest-lead item in the project. Action: ask the Paycom administrator now for API documentation, a sandbox or test company, and whether third-party punch submission is permitted.
- **D13.** The CSV formats are documented in `docs/` with a synthetic example in `fixtures/`. Imported availability is `needs_review` (see D11).
- **D14.** Email content carries the minimum: a link plus a summary of changes. No wages, hours totals or co-workers' details in email bodies.
- **D15.** About 14 weeks from today. Too short for all eight milestones; the spring MVP scope is set in round 5. Homebase keeps running in parallel as a fallback for the first weeks.

## 2026-09-28 – Planning Q&A, round 5

| # | Question (SPEC ref) | Decision |
|---|---|---|
| D16 | Spring 2027 MVP must-haves | Student availability entry (class form + grid, phone and desktop); solver generate + rebalance (engine v1); students view the published schedule and get an email on publish. |
| D17 | Paycom administrator | University HR/payroll, not the library. |
| D18 | Punch relay failure (D12) | Block: if the app cannot confirm the punch reached Paycom, the in-app punch is refused and the student is told to clock in in Paycom directly. The app never holds unsynced punches. |
| D19 | Clock-in location control | Phone + geolocation (geofence). |

### Interpretations

- **D16.** Spring MVP = milestones 1–4, trimmed, plus a minimal student schedule view pulled forward from milestone 6. Printable grids and the hours report are not spring blockers, but pages still get basic print CSS (CLAUDE.md §2.7) and the full printable availability grid follows soon after. Homebase runs in parallel as fallback for the first weeks. Out of spring: engine v2 (duties, tag rules, targeted moves, split, remove from pool, suggestion queries), time off, trades, open shifts, ICS, SAML, time clock, funding imports. **Open:** acceptance scenarios 3 (designated opener) and 4 (leads on separate weeks) are milestone-5 features, but the pilot's real schedule relies on them; confirm whether duty assignments must be in the spring engine.
- **D17.** Punch relay needs central HR/payroll approval, probably with a security review. Start that conversation early; it is the critical path for the time-clock feature, not the code.
- **D18.** A timeout is ambiguous: the punch may have landed in Paycom even though the app saw no reply. On an ambiguous result the app shows "unknown – check Paycom" rather than success or failure, and never retries automatically (to avoid double punches). Exact handling depends on whether the Paycom API supports idempotency keys or a punch lookup.
- **D19.** Geolocation is new personal data (SPEC §11). Conservative defaults: request location only at the moment of punching, with a clear prompt; store only the geofence result (inside/outside) and accuracy, not raw coordinates; state it in the data inventory. Browser geolocation can be spoofed, so treat it as a deterrent and record it as such in the ADR. Check against any HR/Paycom policy on remote punching.

## 2026-09-28 – Planning Q&A, round 6

| # | Question (SPEC ref) | Decision |
|---|---|---|
| D20 | Spring engine scope (§13) | Engine v1 for spring also includes duty assignments with their shape exceptions (scenarios 3, 4) and tag coverage rules (§6.3). Targeted moves, split, remove from pool and suggestion queries stay in v2. |
| D21 | Edit access in the pilot (§1) | One supervisor, an admin, and **student shift leads** with limited rights. |
| D22 | Repository | Private GitHub until the pilot is stable, then public. CI via GitHub Actions. |
| D23 | Dev environment | Docker Desktop (WSL2 backend) on Windows, running the same compose stack as production. |

### Interpretations

- **D20 scope risk.** Spring now covers milestones 1–4 plus the duty and tag-rule parts of milestone 5 plus a minimal student view, in about 14 weeks. The engine is the riskiest part, so it gets built and benchmarked early (acceptance scenarios 1–8, 12, 14 plus 3–4 duties and the tag-rule examples in §6.3).
- **D21 adds a role not in SPEC §1.** Shift leads are students first, and co-workers' availability derives from class schedules (FERPA-sensitive, SPEC §11). Until the maintainer defines lead rights (round 7), the conservative default is: leads have student rights only, plus whatever is explicitly granted; every lead action is audited; anything a lead changes is a draft that a supervisor must publish.
- **D22.** Going public later means the history becomes public: no real data, secrets or internal names ever committed, even while private.

## 2026-09-28 – Planning Q&A, round 7

| # | Question (SPEC ref) | Decision |
|---|---|---|
| D24 | Availability entry (§5.2) | No class-schedule derivation. Employees (students and non-students) enter availability times manually on the weekly grid. |
| D25 | Shift lead rights (D21) | Leads can: view co-workers' free/busy availability; propose schedule edits (drafts); see co-workers' scheduled hours against caps; record day-of call-outs and cover. |
| D26 | Slip plan (D20) | No pre-agreed cut order; re-evaluate scope if spring runs behind. |

### Interpretations

- **D24 changes the spec.** The "classes" entry form and ICS class-calendar import (SPEC §5.2) are removed from spring and moved to the backlog as optional, not deleted from the spec. Availability entry = manual painting on the weekly grid (phone and desktop) plus the CSV import (D13). Course details are never collected, which satisfies §11's minimal-collection rule by default. Availability is still personal employment data and stays access-controlled.
- **D25.** Leads get no access to wages, funding, pay roles' rates, supervisor notes or other employees' personal details. Lead edits are drafts only (CLAUDE.md §2.2); day-of call-out and cover records are audited, flagged for supervisor review, and trigger no notifications until a supervisor publishes. The shift-lead permission is granted per person by a supervisor, separate from the `Shift lead` tag (the tag drives coverage rules; the permission drives access), so a time-bound tag never widens access by accident.
- **D26.** Delivery order puts the engine first so a slip shows up early, while there is still room to re-scope.

## 2026-09-28 – Follow-up

| # | Question (SPEC ref) | Decision |
|---|---|---|
| D27 | Week start (§3.2) | The pilot's workweek always runs Saturday to Friday. |
| D28 | Housekeeping | `SPEC.md` moved to `docs/SPEC.md` (where CLAUDE.md expects it); git repository initialised on `main`. SPEC updated to reflect D1–D27. |

### Interpretations

- **D27.** Saturday is the default week start for new organizations and the pilot's setting, but it stays a configuration value (CLAUDE.md §7). Weekly caps, the two-week planning period and rotation weeks all count Saturday to Friday.

## 2026-10-02 – Rebalancing Q&A, round 1

| # | Question (SPEC ref) | Decision |
|---|---|---|
| D29 | Churn vs fixing rules (§7.3, scenario 14) | A rebalance returns **two what-if options** side by side: (1) the **full fix**, meeting as many rules as possible regardless of how many shifts move; (2) the **best result without a substantial change**, meeting as many rules as possible within a churn limit. The supervisor picks one to apply as a draft. |
| D30 | Published shifts (§9.1) | Locked during rebalance unless the supervisor ticks "allow changes to published shifts" for that run. |
| D31 | Freeze window | Shifts starting within a configurable window (default 48 hours) are never moved by the solver; only by hand. |
| D32 | Hour goals (§6.4) | Students can state desired weekly hours; they only count once a supervisor approves them. Approved desired hours are a soft goal below supervisor-set hour targets. |

### Interpretations

- **D29.** Option 2 is solved lexicographically: the same tiers as the full fix, but subject to a churn budget, so it fixes the most important rules first within that budget. Both options show the validation report, the diff, and per-person hour changes (CLAUDE.md §2.4, §2.5). How "substantial" is measured is round 2. This resolves the scenario 14 ambiguity seen in the benchmark (relaxing a strict rotation moved 25–28% of shifts to fix tag coverage): the full-fix option may exceed 25% churn; the limited option must not.
- **D30.** Locked published shifts are hard constraints in the model (as locked shifts, SPEC §7.1), and the explanation says when a rule couldn't be met because of a locked published shift.
- **D31.** Applies to solver runs only. Manual edits inside the window are allowed (with the normal validation and draft/publish flow). Default 48 hours, configurable per organization; 0 disables it.
- **D32.** New fields: `desired_hours` (per employee per term, a range or single value), with status `pending`/`approved`/`declined` and an audit entry on approval. Unapproved values are shown to the supervisor but ignored by the solver. Priority order becomes: caps (hard) ≫ supervisor hour targets ≫ approved desired hours ≫ fairness.

## 2026-10-02 – Rebalancing Q&A, round 2

| # | Question (SPEC ref) | Decision |
|---|---|---|
| D33 | What "substantial change" measures (D29) | Hours moved. |
| D34 | Priority when approved desired hours exceed available hours (D32) | Work-study (FWS) students with award remaining first, then everyone else. |
| D35 | VPS size (D8) | 4 vCPU. |
| D36 | Rules left unmet in both options (D29) | Explain and suggest: name the rule and the reason, and offer one-click what-ifs (allow published changes, shrink the freeze window for this run, relax that rule for this period). |

### Interpretations

- **D33.** "Hours moved" = the hours of existing shifts that don't survive unchanged: a shift removed or reassigned counts its full length; a retimed shift counts only the part that no longer overlaps its old time. Added shifts filling gaps don't count, since they disrupt nobody. Default limit set in D37.
- **D34.** Work-study priority applies only up to each student's approved desired hours and never past their funding cap (SPEC §10.3) or any other cap. Within each group (work-study and others), hours go to whoever is furthest below their desired hours. Tier order: caps (hard) ≫ supervisor hour targets ≫ approved desired hours for work-study students ≫ approved desired hours for others ≫ fairness. Until award amounts are imported (D10), nobody counts as work-study for this rule, and the explanation says so.
- **D35.** The `worker` container may use all 4 CPUs while solving. Web and database load is light during a solve, so CPU is shared rather than partitioned. The benchmark adds a 3-worker run to confirm results hold with the web app and database competing for CPU.
- **D36.** Suggestions are what-if runs: nothing is saved or published until the supervisor applies an option (CLAUDE.md §2.2). Relaxing a rule "for this period" is recorded with the schedule version and audited.

## 2026-10-02 – Rebalancing Q&A, round 3

| # | Question (SPEC ref) | Decision |
|---|---|---|
| D37 | Default "substantial change" limit (D33) | 30%. |
| D38 | Retime vs reassign | Prefer retiming a student's own shift over reassigning it to someone else. |
| D39 | Rebalance scope (§7.3) | Any selectable span of time up to four weeks, optionally restricted to selected people. Benchmarks stay at two weeks for now. |
| D40 | After a rule change | Run validation immediately, show the new issues, and offer a "Rebalance" button. Nothing changes until the supervisor clicks it. |

### Interpretations

- **D37.** Read as 30% of the scheduled hours inside the rebalance scope (about 90 hours for a full two-week pilot schedule), configurable per organization and per run. In the benchmark, full fixes moved at most about 28% of shifts, so with this default the two options will often be identical; when they are, the screen shows one option and says the full fix is already within the limit, instead of two identical cards.
- **D38.** Implemented as a lower "hours moved" cost for a retimed shift (only its non-overlapping part counts, per D33) plus a small extra cost on reassignment, both in the keep-existing tier. A retime still has to respect availability, the shape rules and the freeze window (D31).
- **D39.** Shifts outside the selected span or people are fixed (treated as locked) but still count toward weekly and period caps, coverage and tag rules, so a partial rebalance can't break the rest of the schedule. Spans over two weeks are allowed but not benchmarked; the solver time limit and the "not proven optimal" label (CLAUDE.md §4) cover them until they are. Selected people: their shifts may change, and the solver may give their hours to anyone eligible (otherwise most moves would be impossible); the diff makes clear who else is affected.
- **D40.** Matches CLAUDE.md §2.4 (validate after every change) and §7 (no automatic changes).

## 2026-10-02 – Housekeeping

- **D41.** `docs/SPEC.md` updated for D29–D40: glossary (desired hours, freeze window), §6.4 desired hours and priority order, §7.3.1 rebalance options and limits, the 4 vCPU performance target, scenario 14 restated in hours moved, and new acceptance scenarios 19–22.

## 2026-10-02 – Benchmark findings

- **D42.** From the solver benchmark (`spikes/cpsat-benchmark/results/SUMMARY.md`), recorded in ADR 0001: CP-SAT with presolve off meets the SPEC §7.3 targets on 4 CPUs; rebalances need the completed warm start; both rebalance options (D29) are solved in parallel on half the CPUs each under one 30-second deadline; a rebalance never returns a result worse than its starting schedule; the model stays linear so CBC remains a fallback. Open: the benchmark should be re-run on the provisioned VPS.

## 2026-10-19 – Stack approved

| # | Question | Decision |
|---|---|---|
| D43 | Stack ADR and license form | ADR 0001 accepted as written. License is **AGPL-3.0-or-later**. |

### Interpretations

- **D43.** `LICENSE` holds the unmodified AGPL-3.0 text from GitHub's license API. The "or later" choice is stated in `README.md` and by the SPDX identifier `AGPL-3.0-or-later` in source file headers. Scaffolding (milestone 1) may start.
- **Open: copyright holder.** The license notice needs a copyright holder. If the code is written as part of university employment, the university may own it under its IP policy. Check with the university before the repo goes public (D22), and before accepting outside contributions decide whether contributors keep their copyright (the usual approach, with a DCO sign-off) or assign it.

## 2026-10-19 – Copyright and repository hygiene

| # | Question | Decision |
|---|---|---|
| D44 | Copyright holder (D43) | The maintainer writes the code personally and unpaid, so the maintainer holds the copyright. |
| D45 | Secrets and personal-data guard | Added: a pre-commit hook (`.githooks/pre-commit`) and a GitHub Actions workflow (`.github/workflows/sensitive-data.yml`) running gitleaks for secrets and `tools/check_sensitive.py` for personal data. |
| D46 | GitHub issues and milestones | To consider later: mirror the open items below and SPEC §13 milestones as GitHub issues and milestones. Not set up yet. |

### Interpretations

- **D44.** Contributors will keep copyright in their own contributions under AGPL-3.0-or-later, the usual arrangement; a contributing guide with a DCO sign-off can come when outside contributions start. The README licence notice can gain a copyright line with whatever name the maintainer wants to publish under.
- **D45.** The hook is per clone: run `git config core.hooksPath .githooks` once (CLAUDE.md §6). Locally gitleaks runs from an installed binary or the pinned Docker image, and is skipped with a warning if neither is available; CI always runs both checks over the full history. `tools/check_sensitive.py` reports file and line only, never the matched text. When the campus ID format is known, add it to `EXTRA_PATTERNS` there.

## Open items for the maintainer

1. Ask university HR/payroll for Paycom API documentation, a sandbox or test company, and whether third-party punch submission and geofenced phone punching are permitted (D12, D17, D19).
2. Share the header row of the Paycom hours report you would import, with a few rows of made-up values (D7, D9).
3. Share the header row of the financial aid FWS award CSV, with made-up values (D10). *Requested from financial aid on 2026-09-28.*
4. Check with campus whether hosting student employee records on a VPS needs a data-processing agreement or registrar/counsel sign-off (D8).
5. Confirm spring 2027 term dates (SPEC §3.2).
6. ~~Confirm who holds copyright~~ Resolved: the maintainer (D44). Optionally add a copyright line with your chosen name to the README notice.
7. Re-run the solver benchmark on the VPS once it is provisioned (D42).
8. Consider mirroring these items and the SPEC §13 milestones as GitHub issues and milestones (D46).
