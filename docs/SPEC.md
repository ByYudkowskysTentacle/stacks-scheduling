# Product and Domain Spec

Working title: **Stacks** (placeholder, see §16). An open-source, self-hosted scheduling and employee management system for libraries staffed mainly by student workers.

This spec was written from a real scheduling exercise at a university library (about 30 student workers, one location, one supervisor). That exercise used Homebase for availability and shifts plus an optimization script to build the schedule. Every rule and scenario below came up in practice. Names in examples are anonymized.

---

## 1. Purpose and users

**Problem.** Library supervisors build student schedules around class timetables, hour limits, funding limits and skill needs (who can catalogue, who can open). General-purpose tools like Homebase handle availability and shift entry, but the hard part (fitting all the rules together, then making small, safe adjustments) happens in the supervisor's head or a spreadsheet.

**Users.**

| Role | What they do |
|---|---|
| Student worker | Enters term availability, sees the published schedule, requests time off, offers or picks up shifts, clocks in and out. |
| Shift lead | A student worker granted extra rights by a supervisor: views co-workers' free/busy availability, proposes schedule edits as drafts, sees co-workers' scheduled hours against their caps, and records day-of call-outs and cover. Never sees wages, funding, supervisor notes or other personal details. Nothing a shift lead changes is published without a supervisor. |
| Supervisor | Configures hours, closures, roles and caps. Generates, edits and publishes schedules. Approves requests and timecards. |
| Admin (library or IT) | Installs and upgrades, manages SSO, departments, supervisors and payroll export settings. |
| Payroll or financial aid office (read-only, optional) | Receives exports. Sees work-study usage reports. |

Employees are mostly students, but non-student employees use the same availability and scheduling features.

**Deployment.** Self-hosted per library or campus. Multi-tenant hosting is out of scope, but don't make choices that would prevent it later (for example, scope every table by `organization_id`). v1 supports one location; keep `location_id` on location-scoped records so more can be added later.

## 2. Glossary

Use these terms exactly, in code and in the UI.

| Term | Meaning |
|---|---|
| Organization | One installation's owner, for example a university library system. |
| Location | A service point with its own hours, for example the main library or a branch. |
| Term | An academic period (semester or quarter) with start and end dates. Availability is scoped to a term. |
| Department | A team with its own schedules and roster, for example Circulation or Special Collections. An employee can be in several; their hours in every department count toward their caps. Supervisors manage the departments an admin assigns them; admins manage all (D95). |
| Planning period | The span a schedule is built for, for example two weeks, for one department at one location. Caps can apply per period. |
| Operating hours | When a location is open, per weekday. |
| Closure | A time the location is closed inside operating hours. It can be recurring (Chapel, Wednesdays 10–11am) or dated (holiday, event). |
| Employee | A person who can be scheduled. Has an employment type, pay roles, tags and caps. |
| Employment type | `regular`, `non_regular` (on the roster but not auto-scheduled) or `inactive`. |
| Pay role | The paid position a shift is worked under, for example "FWS Student Assistant" or "Student Assistant". Carries a wage and a colour. This maps to Homebase "roles". |
| Tag | A skill or duty label used by coverage rules, for example `Cataloguer`, `Processor`, `Marketing`, `Shift lead`, `Opener`, `New employee`. An employee can have many. Tags are separate from pay roles. |
| Availability | An employee's weekly pattern for a term, made of availability blocks. |
| Availability block | A weekday time range of type `unavailable` or `preferred`. Time not covered by an `unavailable` block counts as available. |
| Availability status | `entered`, `not_entered` or `needs_review`. |
| Shift | One person's (or an open) assignment: location, start, end, pay role, status `draft` or `published`, optional note. |
| Open shift | A shift with no assignee that eligible employees can claim. |
| Coverage target | How many people must be on duty per time slot, optionally with tag requirements. |
| Hour cap | A maximum on scheduled hours, per week, per planning period, per term, or derived from funding. |
| Hour target | A soft goal set by a supervisor that raises or protects someone's hours, for example "as close to 20 as possible". |
| Desired hours | The weekly hours an employee says they want for a term. Used by the generator only after a supervisor approves it. |
| Freeze window | A span starting now (default 48 hours) inside which the solver never moves shifts. |
| Duty assignment | A standing rule that a specific employee fills a specific role, for example "Student L opens every weekday". |
| Funding source | Where pay comes from, for example Federal Work-Study (FWS) or department funds. It can have an award amount. |
| Timecard | Actual worked time (clock in and out) used for payroll. |

## 3. Organization configuration

### 3.1 Operating hours and closures

- Per location, per weekday, one or more open intervals. Example from the pilot library:

| Day | Hours |
|---|---|
| Mon–Thu | 7:30am–10pm |
| Fri | 7:30am–4pm |
| Sat | 12–4pm |
| Sun | 6–10pm |
| Wed closure | 10–11am (Chapel) |

- Closures are first-class records: recurring (weekday plus time range, with a term or date range) or dated (a one-off date and time range, or all day). They carry a reason shown in the UI ("Chapel").
- Derived values like "weekly open hours" (73.5 in the example) must account for closures. The pilot changed from 74.5 to 73.5 hours when Chapel was added.
- Operating hours can change per term (finals week, summer). Store them with an effective date range.

### 3.2 Terms and planning periods

- Terms have dates. Availability, caps and funding are term-scoped.
- The planning period length is configurable (1–4 weeks, default 2). Rotation patterns (§6.5) and period caps are defined against it.
- Weeks start on a configurable weekday, defaulting to Saturday. The pilot's workweek always runs Saturday to Friday, and weekly caps, planning periods and rotation weeks all count from the week start.

### 3.3 Time granularity

- 15-minute slots throughout. All shift boundaries, availability blocks and closures snap to slots.
- Entered or imported times off the grid (for example unavailability ending at 8:50am) is kept exactly for display and validation. For scheduling it is rounded toward unavailability, so an 8:50 end blocks until 9:00. The grid entry UI snaps to 15 minutes, so this mainly affects CSV imports.

## 4. People

### 4.1 Employee record

- Name, preferred name, campus ID (optional), email, employment type, hire date, `new_employee_until` date (optional), pay roles with wages, default pay role, tags, funding sources, caps and targets, desired hours (§6.4), notes (supervisor-only).
- `non_regular` employees appear on rosters and in availability views, but the generator never schedules them. They can still be assigned by hand.
- Removing someone from the scheduling pool (a flag, not a delete) must offer to reassign their future draft shifts (§9.4).

### 4.2 Pay roles and wages

- Each shift records the pay role it is worked under. Wage comes from the employee's rate for that role, with a fallback to their default rate.
- Some staff hold one pay role but are scheduled under another. In the pilot, tutors were scheduled as Student Assistants. The UI lets the supervisor choose the pay role per shift and defaults sensibly.

### 4.3 Tags

- Free-form but managed list per organization. Examples: `Cataloguer`, `Processor`, `Marketing`, `Shift lead`, `Opener`, `New employee`.
- Tags drive coverage rules (§6.3), filtering, and badges on every roster and grid view.
- A tag can be time-bound (for example, `Shift lead` for the week of Oct 3 only).
- Tags never grant access. The `Shift lead` tag drives coverage rules; the shift-lead *permission* (§1) is granted per person by a supervisor, separately, so a time-bound tag can't widen someone's access by accident.

## 5. Availability

### 5.1 Model

- Weekly pattern per employee per term: a list of availability blocks (`unavailable` or `preferred`, weekday, start, end, or all day).
- Date-specific exceptions: time off, one-off unavailability, one-off extra availability.
- Availability status: `not_entered` is distinct from "fully available". Views must flag it. The generator must either exclude these people or penalise their use heavily, and must never rely on them to meet coverage without a warning.

### 5.2 Entry

- Employees enter their availability times manually once per term, by painting on a weekly grid, on phone and desktop. Course details are never collected.
- Bulk import from a documented, tool-agnostic availability CSV format (with a matching roster CSV), used to migrate from Homebase or any other tool. Imported availability gets status `needs_review` until the employee or a supervisor confirms it.
- Supervisors can edit on an employee's behalf, with an audit log entry.
- *Backlog (optional, not planned):* a "classes" entry form that turns course times into `unavailable` blocks with a travel buffer, and ICS import of a class calendar.
- An optional availability deadline and reminders are sent only when the supervisor turns them on.

### 5.3 Views

- A **printable availability grid**: one row per employee, one column per day. Each day shows a bar scaled to that day's operating hours, with unavailable (hatched), preferred (solid) and closed (dark) segments, plus text times underneath. The pilot's version of this is the reference design.
- Per employee: open hours inside operating hours per week, days fully out, tags, and scheduled hours for the current period.
- A coverage summary row per day: how many staff are free for all open hours, how many for part.
- Filters by name, tag and availability status.

## 6. Scheduling rules (all configurable)

### 6.1 Shift shape

| Rule | Pilot default |
|---|---|
| Minimum shift length | 2 hours |
| Maximum shift length | 4 hours |
| Maximum hours per person per day | 6 |
| Maximum shifts per person per day | 2 |
| Minimum gap between a person's shifts on the same day | 30 minutes (stops back-to-back shifts turning into one over-long shift) |
| Shifts may not span a closure | Always |

Exceptions attach to a duty assignment or a person. For example, the opener's shift may be 1.5 hours on days they have a 9am class.

### 6.2 Coverage targets

- A default number of people on duty per slot for each location (the pilot used exactly 2 at all open hours).
- Overrides by weekday and time range (for example 3 from 10am–4pm on weekdays).
- Mode: `exact` (penalise both under and over) or `minimum` (penalise under only).
- Coverage shortfall is always the most expensive thing in the objective. The generator should prefer over-coverage to under-coverage, and report both.

### 6.3 Tag coverage rules

Examples the system must support:

- "At least one `Shift lead` on duty during open hours" (or during a time range).
- "At least one `Cataloguer` on duty Mon–Fri 9am–5pm."
- "`Processor` hours total at least 15 per week."
- "A `New employee` is never the only person on duty."

Each rule can be hard (constraint) or soft (weighted). The validation suite reports violations either way.

### 6.4 Hour caps and targets

- **Weekly cap.** Default 20 hours for students (a common campus policy; make it configurable). Per-person overrides, for example 10 hours for someone with other on-campus obligations.
- **Period cap.** For example 12 hours total across a two-week planning period for new employees.
- **Term cap** and **funding cap** (§10.3).
- **Hour targets** (soft): "bring Student I as close to 20 as possible", "raise Student J's hours" or "keep Student J's hours in one week". Targets have a priority, so the supervisor can emphasise one person over another.
- **Protected minimum** (soft): avoid pushing low-hour students lower when rebalancing. In the pilot, taking hours from the busiest people first was consistently what the supervisor wanted.
- **Desired hours** (soft): employees can state the weekly hours they want for a term (a single value or a range). A supervisor approves or declines each one, with an audit entry; pending or declined values are shown to the supervisor but ignored by the generator. When approved desired hours add up to more than the hours available, work-study (FWS) students with award remaining come first, then everyone else; within each group, hours go to whoever is furthest below their desired hours. Work-study priority never goes past a student's desired hours, funding cap or any other cap.
- Priority order: caps (hard) ≫ supervisor hour targets ≫ approved desired hours for work-study students ≫ approved desired hours for others ≫ fairness.
- A cap always beats a target. When a target can't be met, the explanation names the binding cap or availability conflict.

### 6.5 Rotation patterns and duties

- An optional rotation, for example "one week on, one week off" with groups A and B. The pilot started strict and later relaxed it, so support:
  - `strict`: members work only in their group's weeks.
  - `preferred`: off-week shifts are allowed but penalised.
  - `none`.
- Group assignment can be generated (balance each group's ability to cover its weeks) or set by hand.
- Duty assignments: "Student L opens every weekday", "Student Z is shift lead in week 1 and Student X in week 2 (different weeks)", "Lead works 20 hours in their lead week and no more than 10 in the other".

### 6.6 Preferences

- `preferred` availability blocks earn a small bonus.
- Fewer, longer shifts are slightly preferred over many short ones.
- Keeping existing shifts (warm start) is rewarded when editing (§7.3).

## 7. Scheduling engine

### 7.1 Inputs and outputs

- **Input:** organization config, planning period, employees with availability, rules, existing shifts (draft and published), locked shifts, and a solve mode.
- **Output:** a proposed shift set, a diff against the current schedule, per-person hours before and after (per week and per period), the validation report (§8), solver status (optimal, feasible with gap, or infeasible), and explanations (§7.4).

### 7.2 Model (reference approach that worked in the pilot)

- Enumerate candidate shifts per person and day: every start on a slot boundary and every allowed length that fits inside operating hours, avoids closures and doesn't overlap `unavailable` blocks. Add special candidates for duty exceptions.
- Binary decision per candidate. Coverage per slot is the sum of the candidates covering it, plus shortfall and overage slack variables.
- Hard constraints: availability, shift shape (§6.1), caps (§6.4), duties (§6.5), locked shifts, and hard tag rules.
- Objective in priority tiers: coverage shortfall ≫ hard-ish tag rules ≫ over-coverage ≫ hour targets ≫ desired hours (§6.4) ≫ fairness (minimise the highest hours among general staff, and penalise cuts to low-hour staff) ≫ keeping existing shifts (retiming preferred over reassigning) ≫ fewer shifts ≫ preference bonus.
- Candidate starts every 30 minutes were enough in practice and roughly halved the problem size. Keep 15-minute starts available as an option.

### 7.3 Modes

| Mode | Behaviour |
|---|---|
| Generate | Build a period from scratch (existing drafts ignored or replaced after confirmation). |
| Rebalance | Re-optimise with a strong keep-existing bonus. Returns two what-if options (§7.3.1). This was the most-used mode. |
| Targeted move | "Move about 2 hours from X, Y or Z to W" or "give W more hours". Reassigns whole shifts where feasible, and reports why a donor couldn't give (for example, all of Y's shifts clash with W's classes). When no set of whole shifts comes within 30 minutes of the ask, it also offers splits: the start or end of one donor shift goes to W, and the donor keeps the rest (D102). |
| Remove from pool | Reassign a person's future shifts to eligible others at the same times, preferring low-hour and preferred-time matches. |
| Split shift | Split one shift into parts (for example a 4-hour shift into two 2-hour halves) and assign each part. |
| What-if | Any mode without saving. It returns the diff and hour changes so the supervisor can compare options. |

#### 7.3.1 Rebalance options and limits

- **Two options.** Every rebalance returns two options side by side, each with its diff, validation report and per-person hour changes:
  1. **Full fix:** meets as many rules as possible, however many hours move.
  2. **Limited change:** meets as many rules as possible, in priority order, while moving no more than the change limit.

  The supervisor applies one as a draft, or neither. When the full fix is already within the limit, the screen shows a single option and says so.
- **Hours moved** measures change: a shift removed or reassigned counts its full length; a retimed shift counts only the part that no longer overlaps its old time; added shifts don't count. The change limit defaults to 30% of the scheduled hours in scope, configurable per organization and per run.
- **Retime before reassign.** When hours must move, prefer moving a person's own shift within their availability over giving it to someone else.
- **Published shifts are locked** unless the supervisor allows changes to them for that run.
- **Freeze window.** Shifts starting within the freeze window (default 48 hours, configurable, 0 to disable) are never moved by the solver. Manual edits there are still allowed.
- **Scope.** Any span up to four weeks, optionally limited to selected people. Shifts outside the scope are held fixed but still count toward caps, coverage and tag rules. Hours from selected people may go to anyone eligible, and the diff shows who else is affected.
- **Never worse.** A rebalance never returns a result that scores worse than the schedule it started from. If the solver doesn't beat it within the time limit, it says so and keeps the existing schedule.
- **Unmet rules.** If a rule can't be met in either option, the screen names the rule and the reason and offers one-click what-ifs: allow changes to published shifts, shrink the freeze window for this run, or relax that rule for this period (recorded with the schedule version and audited). When a hard tag rule leaves a coverage gap, the screen says which rule and who was free but kept off by it, and the what-if treats just that rule as a preference for that run, so coverage comes first. The saved rules don't change, and the validation suite still reports each place the rule is broken (D97).
- **After a rule change,** the validation suite runs immediately and the new issues are shown with a "Rebalance" button. Nothing changes until the supervisor clicks it.

#### 7.3.2 Running solves

- Solving runs in the background with a time limit and progress, and must not block the UI.
- Performance target for the pilot size (about 30 people, 2 weeks, about 150 hours a week of coverage) on a 4 vCPU server: a rebalance returns in under 30 seconds, and a full generation in under 5 minutes. Record benchmarks in CI. Spans over two weeks are allowed but not yet benchmarked.

### 7.4 Explanations

- Per person: "Low hours because available only 12 of 73.5 open hours", or "Low hours because shifts went to higher-priority targets; could take X, Y or Z".
- Per unmet target: which cap, duty or availability conflict blocked it.
- For forced trade-offs: for example, "Wednesday 8–9am has 3 people because the opener leaves at 9 and every other shift must be at least 2 hours; the alternative is a 1-hour shift."

### 7.5 Suggestion queries (interactive helpers)

These were the most valuable interactions in the pilot and should be one click away:

- **Who could take this shift?** List eligible people with their current hours, whether it's a preferred time, and which rule would stop anyone else.
- **How can I raise this person's hours?** List shifts they could take over, grouped by "same week" and "either week", noting whether the current holder has hours to spare or is low themselves.
- **Who could cover this gap?** The same query for an open or uncovered slot.

## 8. Validation suite

Run after every mutation. Show results in the UI and store them with the schedule version.

1. Coverage: every open slot meets its target, with under and over reported separately. Closures must have zero people on duty.
2. Availability: no shift overlaps the assignee's `unavailable` block or approved time off.
3. Shape: length, daily hours, shifts per day and gap rules.
4. Caps: weekly, period, term and funding. Show projected funding burn.
5. Duties and tag rules.
6. Double booking across locations.
7. Status: people with `not_entered` availability who are scheduled get a warning.
8. Drift: shifts that differ from the last published version, for the publish summary.

## 9. Schedule workflow

### 9.1 Drafts and publishing

- All edits create draft changes. Publishing is explicit, per period or per location, and shows a summary (added, changed and removed shifts per person) before confirming.
- Schedule notifications go out only on publish, and only to affected employees. The one other kind is a decision on a person's own request (time off, an open-shift claim, cover): the people who asked are told the outcome, and a shift change it causes still reaches the published schedule only when a supervisor publishes (D103). Every notification appears in the in-app list; email over SMTP (settings from the environment) follows each person's email settings, on by default for published schedules and availability reminders and off for decisions. SMS and push are later. Emails carry a link and a summary of the employee's own changes, never wages, hour totals or co-workers' details.
- Published schedules keep a version history. Supervisors can compare versions and roll back drafts.

### 9.2 Employee self-service

- View own shifts (web and phone, plus an ICS feed).
- View the full published schedule, with co-workers' preferred names on shared shifts. Students never see co-workers' availability, hours, wages, tags or notes (shift leads see free/busy and hours against caps, per §1).
- Request time off (the approval flow checks coverage impact first).
- Offer a shift for trade or cover, or claim an open shift. The supervisor approves, and the validation suite runs on the would-be result.

### 9.3 Bulk operations

- Every bulk change (a solver result, import or split) applies as one transaction: all or nothing, with a single audit entry and an undo.

### 9.4 Safe destructive actions

- Prefer retime and reassign over delete. When deletion is needed, confirm with a list of what will go, and keep the records soft-deleted and restorable for a set period.

## 10. Time tracking, payroll and funding

Paycom is the payroll system of record. It is administered by university HR/payroll, not the library, so any API access needs their approval.

### 10.1 Time tracking and punch relay

- Today students punch in Paycom directly. The goal is a single clock-in: employees clock in and out in this system, and each punch is relayed to Paycom so they are clocked in on both.
- Relay goes through a `PunchRelay` interface with a Paycom adapter and a fake for tests. The Paycom adapter is built only against API documentation and credentials obtained from the Paycom administrator. Paycom doesn't publish open developer documentation, so this feature is blocked until access is granted.
- **Failure blocks the punch.** If the system can't confirm the punch reached Paycom, it refuses the in-app punch and tells the employee to clock in in Paycom directly. It never holds unsynced punches.
- **Ambiguous results.** A timeout may mean the punch landed. On an ambiguous result, show "unknown, check Paycom" rather than success or failure, and never retry automatically (to avoid double punches). Use idempotency keys or a punch lookup if the Paycom API offers them.
- **Location control.** Phone punches are allowed inside a configurable geofence around the location. Location is requested only at the moment of punching, with a clear prompt, and only the result (inside or outside, plus accuracy) is stored, never raw coordinates. Browser geolocation can be spoofed, so treat this as a deterrent, and check it against any HR or Paycom policy on remote punching.
- Timecards compare scheduled against actual. Missed punches and unscheduled work are flagged for the supervisor.

### 10.2 Paycom data flows

- **Actuals import (first).** The supervisor imports a Paycom hours or timecard report (CSV) each pay period, through an importer interface with a fake. The column mapping is configurable and built from the real report's layout; the sample under `fixtures/paycom/` uses that layout with synthetic values. Imported actuals are the only source of "actual spend" (§10.3) and are used to reconcile in-app punches against Paycom.
- **Hours file export (deprioritised).** Because punches already reach Paycom, a file export of hours is only needed if the Paycom administrator asks for one (for example, for corrections). Keep a `PayrollExporter` interface; build a file adapter only against an agreed import template, never a guessed one. If built, it covers approved timecards only (never scheduled hours) for one pay period, maps pay roles to Paycom pay codes, records a checksum and an audit entry, and never exports the same timecard twice without an explicit re-export.

### 10.3 Funding (work-study)

- Funding source per employee per term, with an award amount (for example an FWS award) and an optional split across sources.
- Award amounts come from a CSV provided by financial aid each term, imported with a configurable column mapping as one transactional bulk operation (§9.3). The fixture is synthetic and matches the real layout.
- Projected spend = scheduled hours × wage, and actual spend = imported Paycom hours × wage (§10.2). Until actuals are imported for a period, views show projected spend and label it as a projection. Show the remaining award, the weeks of work left at the current pace, and alerts at configurable thresholds (for example 75% and 90%).
- The generator can turn remaining award into a funding cap for the period.

## 11. Privacy, security and compliance

- Student employee records (availability, wages, work-study awards, hours) may be protected education records at US institutions under FERPA. Treat them that way by default, and document the data inventory so each campus can confirm with its registrar or counsel.
- Minimal collection: availability is stored only as time blocks, and course details are never collected. Punch geolocation is stored only as an inside/outside result (§10.1).
- Hosting on a third-party VPS may need a data-processing agreement or registrar/counsel sign-off. The install guide flags this.
- Role-based access (§1), server-side authorization, an audit log of every write, encryption in transit, and database encryption at rest where the platform allows it.
- Retention settings per data type (for example, purge availability 1 year after the term ends). Export and delete a single person's data on request.
- Authentication through SAML 2.0 and OIDC for campus SSO, plus local accounts with strong password rules and optional TOTP. Order: local accounts first, then Google Workspace (the pilot campus) through a generic OIDC configuration restricted to the campus domain (checked server-side), then SAML. Other OIDC providers are configuration, not code. Tests use a local Keycloak container, never a real identity provider.

## 12. Acceptance scenarios

Each of these must be an automated test with a synthetic fixture. They are drawn directly from the pilot. "Student A" and so on are placeholders.

1. **Base coverage.** 28 students, 2 weeks, operating hours as in §3.1, exactly 2 people on duty per slot. Expect zero shortfall, and nobody over 20 hours in any week.
2. **Closure.** Add the Wednesday 10–11am closure. Expect no shift spanning 10–11am, and weekly open hours of 73.5. When the opener leaves at 9am and shifts must be at least 2 hours, expect one hour of over-coverage from 8–9am, reported with an explanation, rather than a 1-hour shift (unless the supervisor configures otherwise).
3. **Designated opener.** Student L starts at opening time every weekday in both weeks. On Mon, Wed and Fri their shift is 1.5 hours (class at 9am), as a duty exception to the 2-hour minimum.
4. **Leads on separate weeks.** Students X and Z are shift leads. Each works 20 hours in their own lead week and no more than 10 in the other, and the lead weeks differ.
5. **New-employee period cap.** Students tagged `New employee` total no more than 12 hours across the two weeks. Two of them also have a 10-hour weekly cap.
6. **Priority target.** Student I, with the target "as close to 20 as possible", reaches 20 hours without breaking coverage, moving as few hours as possible: little more than the hours Student I gains (D98). Who gives up the hours follows from the fewest changes; among equally small changes, the busiest give first.
7. **Non-regular exclusion.** Four `non_regular` employees are never auto-scheduled.
8. **Not-entered availability.** A student with `not_entered` status is flagged in every view, and is scheduled only when needed and with a warning.
9. **Remove from pool.** Removing Student W reassigns their three draft shifts to eligible people at the same times, with no deletions, and coverage stays intact.
10. **Targeted move.** "Move about 2 hours from Students D, T or P to Student J, keeping J in one week." Only one of P's shifts is feasible. The response explains that none of T's shifts fit J's class times and that all of D's shifts are in the other week.
11. **Split shift.** Split Friday 12–4pm into 12–2pm and 2–4pm and assign each half to a different person. Coverage and caps still pass.
12. **Relaxed rotation.** Switching the rotation from `strict` to `preferred` lets people work in both weeks, and a rebalance changes only a small fraction of shifts.
13. **Opportunities query.** "How can I raise Student E's hours?" lists the takeable shifts with each current holder's hours, grouped by week, and excludes shifts protected by duties or targets.
14. **Minimal churn.** After any rule change, the limited-change option moves no more hours than the change limit (§7.3.1), and the full-fix option reports how many hours it moves. With the limit set to 25%, the limited-change option keeps at least 75% of existing scheduled hours.
15. **Pay role on reassignment.** Reassigning a shift to someone with a different default pay role updates the shift's pay role and colour.
16. **Publish gate.** No notification is sent for any draft change. Publishing sends exactly one summary per affected employee.
17. **Shift lead drafts.** A shift lead's edits and day-of call-out records stay drafts, send no notifications, and appear in the supervisor's review queue. The shift lead can't see any co-worker's wage, funding or notes.
18. **Punch relay failure.** With the fake `PunchRelay` set to fail, an in-app punch is refused with an instruction to clock in in Paycom, and no local punch is kept. Set to time out, the punch shows "unknown, check Paycom" and is not retried.
19. **Two rebalance options.** After relaxing a strict rotation, a rebalance returns a full-fix option and a limited-change option. The limited option moves no more than the limit, and each option lists the rules it leaves unmet with a reason and one-click what-ifs. When both options are identical, one is shown with a note.
20. **Locks.** A rebalance never moves a published shift unless allowed for that run, never moves a shift inside the freeze window, and never changes shifts outside the selected span or people.
21. **Desired hours.** Pending desired hours are ignored. When approved desired hours exceed the hours available, a work-study student with award remaining gets their hours before others, without passing their funding cap.
22. **Never worse.** When the solver can't beat the existing schedule within the time limit, the rebalance keeps it and says so.

## 13. Milestones

1. **Foundations.** Stack ADR, auth (local accounts first), organization, location, operating hours, closures, employees, tags, pay roles, audit log.
2. **Availability.** Term availability entry on the weekly grid, `not_entered` tracking, the printable availability grid, and roster/availability CSV import.
3. **Manual scheduling.** Shift CRUD with drafts, the week and period grid, the validation suite, publish with email notifications, version history, and shift-lead permissions.
4. **Engine v1.** Candidate model, coverage, shape rules, caps, duties, tag rules, desired hours, generate and rebalance (both options, locks, freeze window, scope), what-if, and explanations. Acceptance scenarios 1–8, 12, 14 and 19–22.
5. **Engine v2.** Targeted moves, split, remove from pool, and suggestion queries. Scenarios 9–11, 13 and 15.
6. **Self-service.** Student phone views, time off, trades, open shifts and ICS feeds.
7. **Time and pay.** Paycom actuals import, FWS award import and funding tracking, then clock in and out with Paycom punch relay once API access is granted. Scenario 18.

**Spring 2027 release (target: start of the spring term).** Milestones 1–4, plus the student view of the published schedule from milestone 6, with Homebase kept as a frozen fallback at first: Stacks is the schedule students use, and a printable and CSV export of the published schedule supports re-entry into Homebase if a fallback is ever needed (D47). The full printable availability grid may follow shortly after go-live. If the schedule slips, scope is re-evaluated at that point.
8. **Hardening.** SSO, retention, accessibility audit, a Docker install guide, backup and restore, and upgrade migrations.

## 14. Reports and views

- Availability grid (printable, §5.3).
- Schedule grid: week and period, by person or by time, with coverage counts per slot, tag badges and closure bands.
- **Scheduled hours vs limits:** per person, hours per week and for the period, the limit that applies, and a bar showing hours against that limit. The pilot's version is the reference design.
- Coverage report: gaps and over-coverage per day, and tag coverage.
- Funding burn-down per student and in total.
- Payroll export history.
- Every report exports to CSV and JSON and prints cleanly.

## 15. Notes from Homebase (reference, not a dependency)

What the pilot saw in Homebase's schedule data, which is useful for an optional import tool and as a checklist:

- Jobs link a user to a location, with a level, default role and wage rate.
- Roles (pay roles) have names and colours.
- Shifts have start and end, an owner (a job, or the location for an open shift), a role, a published flag, a note, and claim, trade and cover indicators.
- Availability is stored as dated instances of weekly blocks titled Unavailable or Preferred, plus an all-day flag.
- Conflicts between shifts and availability are computed server-side.

Improvements to aim for over what the pilot experienced:

- Multi-week caps, funding caps and tag-based coverage rules as first-class settings.
- A built-in generator with minimal-churn rebalancing and what-if comparisons.
- Clear totals: hours against limits, and coverage counts, visible without scrolling a virtualized grid.
- A printable availability grid.
- Bulk, transactional edits with undo.
- A documented, stable API for everything the UI does.

## 16. Open questions for the maintainer

Answers and their interpretations are logged in `docs/decisions.md` (D-numbers below).

1. Project name: **open**. License: AGPL-3.0 (D3).
2. Clock in and out: not in v1; later, in-app punches relayed to Paycom (D2, D12, D18, D19).
3. Paycom: **open**. API access requested through university HR/payroll (D17); the actuals report layout is still needed (D9).
4. Work-study awards: a financial aid CSV each term (D10). Sample layout requested.
5. Locations: one location first (D1).
6. SSO: Google Workspace via OIDC, with generic OIDC and SAML to follow (D5).
7. Co-worker visibility: preferred names on the published schedule (D6).
8. Spring 2027 term dates: **open**.
