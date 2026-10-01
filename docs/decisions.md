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

## Open items for the maintainer

1. Ask university HR/payroll for Paycom API documentation, a sandbox or test company, and whether third-party punch submission and geofenced phone punching are permitted (D12, D17, D19).
2. Share the header row of the Paycom hours report you would import, with a few rows of made-up values (D7, D9).
3. Share the header row of the financial aid FWS award CSV, with made-up values (D10). *Requested from financial aid on 2026-09-28.*
4. Check with campus whether hosting student employee records on a VPS needs a data-processing agreement or registrar/counsel sign-off (D8).
5. Confirm spring 2027 term dates (SPEC §3.2).
