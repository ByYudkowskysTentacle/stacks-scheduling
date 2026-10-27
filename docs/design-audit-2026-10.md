# Design and usability audit, October 2026

The whole site as it stood at `main` @ 782adff, reviewed on 2026-10-21.

**How it was done**
- Every distinct page was crawled as an admin (71 page types) and a student (11), with GET requests only, on the demo instance (production build, synthetic data, a generated and published two-week schedule).
- Each page was captured full-height at **1280px** (desktop) and **390px** (phone), plus print-media renders of the schedule pages and the availability grid.
- On every capture: **axe-core 4.10** (WCAG 2.1 A/AA plus best practice), horizontal overflow, tap targets under 24px, heading structure and font sizes.
- Contrast ratios computed for every colour token.
- Three reviewers worked through the screenshots and templates (student flow, supervisor scheduling flow, people/settings/admin), using a standard critique framework: first impression, usability, hierarchy, consistency, accessibility, copy.
- Key root causes were checked in the code or in a browser before being listed here.

Severity: **Critical** = wrong information, data-loss risk, a broken layout or a WCAG failure on a core task. **Moderate** = real friction on a frequent task. **Minor** = polish.

## Summary

The foundation is good. Text contrast is 6.3:1 or better everywhere. axe-core found one minor issue across 164 page captures. State is never shown by colour alone. Most destructive actions need a labelled confirmation. The copy is plain and honest about drafts, privacy and consequences.

The problems are concentrated:

1. **Some screens give wrong or missing answers.** Shifts beyond a fixed 2–4 week window vanish from the student views. The day page and the grid's coverage row say "OK" and "Covered" while skill rules are unmet.
2. **Phones.** The phone availability painter can mark times by accident while scrolling. Five pages scroll sideways because of a one-line CSS bug. The header takes 23% of the screen on every page.
3. **A few destructive or privileged actions skip the product's own confirmation rule.** These are the rules-page removals, the retention save, Admin role changes and the unreachable closure delete.
4. **The design system is unevenly applied.** Django and allauth default form markup leaks through on about 15 pages, there are 12 font sizes, settings feels like separate pages, and noisy action bars flatten the hierarchy.

## Part 1: Bugs and safety (no design choices involved)

| # | Finding | Where | Severity | Fix |
|---|---|---|---|---|
| 1.1 | **Upcoming shifts vanish beyond a fixed window.** Home looks 2 weeks ahead and My schedule 4 weeks. A student with shifts 13 weeks out sees "No published shifts", even right after a notification listed them. The published view opens on the current week with no hint where shifts are. | home, my-schedule, published, shifts-exchange | Critical | Show the next N published shifts whatever their date. Empty states should point to the next shift ("Your next shift: Mon 18 Jan 7:30am →"). Published opens on, or links to, the nearest week that has shifts. |
| 1.2 | **The phone availability painter acts on `pointerdown`.** A scroll swipe that starts on the grid sets the start point, and a second swipe in the same column paints a "Can't work" range. The grid scrolls inside a nested 70vh box, so there's almost nowhere else to scroll from. | availability (student), people-n-availability | Critical | Paint on a real tap (`click`, or `pointerup` with no `pointercancel` and little movement). Clear the start point on `pointercancel`. On phones, let the page scroll instead of a nested box. |
| 1.3 | **Coverage looks fine when it isn't.** The day page marks every slot "OK", and the grid's coverage row says "Covered", while the validation suite reports unmet skill (tag) rules on those days. Both count heads only. | schedule-n-day-*, schedule-n footer | Critical | Show tag-rule misses per slot on the day page and as "N skill rules missed" in the grid footer, linked to the validation report (CLAUDE.md §2.4). |
| 1.4 | **Five pages scroll sideways on phones** (period grid +576px, takers +144px, raise, departments, members). Cause: `.visually-hidden` text is `position:absolute` inside scroll boxes that aren't positioned, so it escapes them. | phone | Critical (WCAG 1.4.10) | `.grid-scroll, .data-table { position: relative; }`. Verified in Chromium: period scroll width goes from 995 to 390px. |
| 1.5 | **Rules-page removals have an unlabelled required checkbox.** Every cap, override, tag rule, rotation, duty and target has a bare checkbox next to "Remove". Its only label is hidden and reads "Confirm removal" on every row. Pressing Remove alone fails, with the error at the top of a 7,000px page. | settings-rules | Critical (WCAG 3.3.2) | A visible, specific label ("Yes, remove Oakley's 10h cap"), or a confirm page like the other deletes. Return to the same section with the message there. |
| 1.6 | **Saving retention periods sets up an irreversible nightly purge with no preview.** The "due for purging" counts only reflect periods already saved. | settings-retention | Critical (CLAUDE.md §2.3) | On save, if anything would be purged, show the counts ("Saving will delete 412 availability blocks tonight") and require a confirmation tick. |
| 1.7 | **Closures can't be removed.** `closure_delete` exists but no page links to it. | settings-locations-* | Moderate | Add Remove to the closure edit page, as operating hours have. |
| 1.8 | **Promoting someone to Admin** (access to all student records and wages) is one "Change" click with no confirmation. The admin's own row offers a select that can only error. | settings-members | Moderate | Confirm changes to and from Admin. Show "You (admin)" on your own row. |
| 1.9 | **The focus ring fails contrast.** Yellow `#ffbf47` is 1.64:1 on white (3:1 required, WCAG 1.4.11), so keyboard focus is hard to see on most pages. | everywhere | Critical (WCAG) | A two-tone focus style: yellow fill or outline plus a dark inner ring (the GOV.UK pattern), at least 3:1 on both light and blue backgrounds. |
| 1.10 | **Pay role is shown by colour alone.** It appears only as the shift chip's left-bar colour on the grid, the published week and My schedule. The grid legend shows only blue, which suggests the colour means state. | schedule-n, published, my-schedule | Moderate (WCAG 1.4.1) | Show the role name (and location) as text on public shifts. Add a pay-role key to the grid legend and the role to the chip's screen-reader text. |
| 1.11 | **"Send reminders now" is primary and enabled when no one listed can receive email** (none have sign-in accounts). | availability-reminders | Moderate | "Will email N of 3". Disable at 0. Link names to their records and offer Invite inline. |

## Part 2: Structure and usability

### Navigation and home
| # | Finding | Severity | Recommendation |
|---|---|---|---|
| 2.1 | On a 390px phone the header is 194px (23% of the screen). 9–10 links wrap onto 3–4 rows on every page. | Moderate | Collapse the nav on phones (see question 2). |
| 2.2 | **Supervisor home is only a setup checklist.** Once everything is "Done" it says nothing useful. | Moderate | A "needs attention" home: current periods with unpublished changes, review queue, time-off requests, availability not entered, coverage and skill gaps, each linking to the fix. Collapse the checklist when complete. |
| 2.3 | **Student home** doesn't single out the next shift (no "Today"/"Tomorrow"), and it hides things needing an answer (swap requests sent to them) on the Open shifts page. | Moderate | A "Next shift" block (relative day, time, location, role), a "Needs your answer" card, and a count of open shifts. |
| 2.4 | Labels drift: "Schedule" / "Everyone's schedule"; "Open shifts" / "Open shifts and cover" / "Offer a shift or pick one up"; "Account" opens "Change Password"; Desired hours and Time-off review highlight Schedule instead of Review. | Minor | One label per destination across nav, h1 and links. An Account overview page (email, password, two-factor, email settings). Fix the `aria-current` conditions. |

### Supervisor scheduling
| # | Finding | Severity | Recommendation |
|---|---|---|---|
| 2.5 | The period grid has no sticky header or name column. It's about 1,950px tall on desktop, and on phones the names scroll away when panning. | Moderate | Sticky day headers and person column inside a viewport-height scroll box. |
| 2.6 | **The period page is noisy.** 7 equal-weight buttons; "Raise hours" 33 times (also offered to people at cap or outside the pool, leading to a dead end with no reason); 231 "+" cells; "Review and publish" stays primary when nothing is pending. | Moderate | Show "Raise hours" only where it can work, as a small link in the hours cell. Group the actions into Edit (Generate/rebalance, Move hours, Add a shift) and View (Versions, Reports, Print). Show "Up to date" when nothing is pending. |
| 2.7 | **After saving a shift**, the page jumps to `#validation`, scrolling the confirmation out of view. It repeats the whole-period total, so you can't tell whether your edit added an issue. A warnings-only report looks like a neutral card. | Moderate | Put the message next to the validation card. Report the change ("15 issues, no change" or "+1 coverage gap Tue 2–2:30pm"). Give a warnings report an accent. |
| 2.8 | **The day page** is 58 near-identical 15-minute rows, with the actionable shift list below them. | Moderate | Merge identical runs into ranges and put the shift list first. |
| 2.9 | **The takers page on phones:** the "Give it to X" button is the last column, off-screen. "Can't take it (23)" is fully expanded (about 2,000px), with the name repeated in every reason. | Moderate | Action in the first column (or cards on phones). Blocked list in a disclosure. |
| 2.10 | **Move hours and Generate:** 30-checkbox single columns with no hours shown. Advanced solver settings are always visible. A checkbox label is detached. | Moderate | "Riley (18h this period)" labels in a multi-column grid. An "Advanced" disclosure. Checkbox inside its label. |
| 2.11 | **Edit shift:** the h1 doesn't say whose shift or when. Times show as "14:00" while the grid says "2pm". Remove sits right next to Cancel. | Moderate | A specific heading with a Draft/Published badge, 12-hour times, destructive actions grouped last. |
| 2.12 | **Print:** the grid prints one week with no week label; validation groups print collapsed (no list); columns are equal, so names are cramped. Back-links and "Record a call-out" print. | Moderate | A printed week caption, validation groups expanded for print, a `<colgroup>` with a wider name column, `no-print` on back and action links. |
| 2.13 | **Versions detail** is a flat 130-row list (about 4,700px). | Minor | Reuse the people × days grid, or group by day. |

### Student availability and requests
| # | Finding | Severity | Recommendation |
|---|---|---|---|
| 2.14 | "Save availability" is about 1,300px below the painter on phones, with no unsaved-changes cue. | Moderate | A Save button directly under the painter, an "Unsaved changes" status line, and a leave-page warning. On phones, collapse "Times by day" into "Type times instead". |
| 2.15 | Painter rows are 13.5px tall on phones. The only cue after the first tap is a tiny outline, and the legend is off-screen while painting. | Moderate | Taller rows (or 30-minute rows) on phones, a "Start: Mon 9:15am, now tap where it ends" status line, and swatches in the brush buttons. |
| 2.16 | **Mixed time formats.** The grid uses "1pm", but the help text, placeholders and JS write-back use 24-hour time. "1:00-2:15" for a 1pm class becomes 1am. | Moderate | 12-hour times everywhere a person reads or types them (the parser already accepts am/pm). |
| 2.17 | **Time off:** "All day" is ticked by default, but the time fields stay visible; the checkbox sits under its label. | Moderate | Hide the time fields when "All day" is ticked; checkbox before label. |

### People, settings and the availability grid
| # | Finding | Severity | Recommendation |
|---|---|---|---|
| 2.18 | **Settings doesn't feel like one area.** The sub-nav disappears on Organization and on every add or edit page, with no back-links. | Moderate | Settings sub-nav on every settings page, plus back-links. |
| 2.19 | **Scheduling rules is 7,000px.** Seven add forms are always expanded, there's no in-page navigation, and existing items can only be removed, not edited. | Moderate | Add forms behind "Add a…" disclosures, existing items in compact tables with Edit/Remove, and a jump list (or sub-pages). |
| 2.20 | **Raw form rendering on about 15 pages** (Django default `as_div`/`as_field_group` and allauth defaults). Trailing colons, "---------" choices, detached checkboxes, unmarked required fields, 20-character inputs that truncate emails. Operating-hours help text sits under the wrong day. | Moderate | One project-wide form field template (wrapper, inline checkboxes, no colons, "(required)", sensible widths) via `FORM_RENDERER`, plus styled allauth templates. This fixes most of these pages at once. |
| 2.21 | **The employee form** is one ungrouped 2,200px column, with the wage far from the pay-rate fieldset. | Moderate | Fieldsets: Identity · Employment and scheduling · Pay · Departments and tags · Notes. |
| 2.22 | **The employee record** shows nothing scheduling-related (caps, duties, targets, rotation, availability status). These live only on the 7,000px rules page. | Moderate | A "Scheduling" section on the record, linking to each rule. |
| 2.23 | **Availability grid:** day columns of unequal width (the same hours look different) and no time axis. The print copy doesn't say which filters were applied, and its legend is incomplete. "Needs review" looks like a skill tag. The name column scrolls away on phones. The pager drops the filters. | Moderate | Equal columns with each day's open hours in the header, faint tick marks, a print context line, a complete legend, distinct status badges, a sticky name column, and filters kept in the pager. |
| 2.24 | **Audit log:** 21 pages with no filtering. Rows say "availability block #719" rather than the person. | Moderate | Filters (person, type, who, dates), names instead of ids, and bulk system writes collapsed into one row. |

## Part 3: Visual system and copy

| # | Finding | Severity | Recommendation |
|---|---|---|---|
| 3.1 | 12 distinct font sizes (11.2–25.6px). h3 at 1.05rem reads as body text. | Moderate | A 5–6 step type scale as tokens (for example 0.8 / 0.9 / 1 / 1.15 / 1.35 / 1.75rem) with clear heading weights. |
| 3.2 | Cards have about 33–48px of dead space above their first heading (card padding plus h2 margin). | Minor | `.card > :first-child { margin-top: 0 }` and a spacing scale (4/8/12/16/24/32px). |
| 3.3 | The header is 72rem wide but wide pages are 100rem, so the brand and nav don't line up with the content. | Minor | One container width per page, shared by header and main. |
| 3.4 | Status badges ("Not entered", "Needs review") look like tags (dashed pills). | Moderate | Distinct status styling (square corners, filled, with a marker). Keep dashed pills for tags. |
| 3.5 | Empty states are heavy: seven 100px "No published shifts" cards; empty tables shrink to about 150px with headers over "None yet". | Minor | Compact empty states (one line), and plain text instead of empty tables. |
| 3.6 | Developer text leaks into the UI: "(SPEC §11)", "(D95)", "SPEC example: 365", "Is active:", "Color" vs "Colour", hex codes as list content, Title Case on allauth pages. | Minor | Remove spec and decision references from help texts, use sentence case and one spelling, show swatches with names. |
| 3.7 | Notification cards repeat the email: "Stacks demo:" prefix, a plain-text URL and a vague "Open" link inside `<pre>`. | Minor | Strip the prefix and URL for in-app display. Specific link text. Lists as lists. |
| 3.8 | "Nobody is notified until you publish" is the most important reassurance on the scheduling pages, but it's in grey 0.9rem help text. | Minor | Show it next to the Draft banner in body text. |

## What works well (keep it)

- **State never relies on colour alone.** Hatched, solid and dark fills; "NEW/CHANGED" chip text; "Done:"/"Problem:" message prefixes; "You" on your own shifts; the Draft/Published banner.
- **Drafts before publishing is consistent and visible.** The publish page lists each person's changes and flags who won't be notified.
- **Explanations are strong.** Who could take a shift, and why others can't; split and raise reasons; solver results framed as what-ifs.
- **Privacy-minded copy where it matters.** "Don't enter course names", what emails contain, warnings on records exports.
- **Progressive enhancement.** Text fields are the source of truth for availability, the painter is an add-on, and there's no build step.
- **The availability grid and schedule grid print well** in landscape, apart from the gaps in 2.12 and 2.23.
- **Text contrast is excellent** (6.3:1 to 16.6:1) and the token set is small and consistent.

## Notes on the evidence

- The capture browser's system font had no bold face, so weight-based hierarchy couldn't be judged from screenshots. It was checked in the CSS instead.
- Not captured: the publish page with pending changes, the solver results page, and filled-in student request states. Findings there come from the templates.
- Raw captures, axe results and reviewer notes are in the session scratchpad. They weren't added to the repo.
