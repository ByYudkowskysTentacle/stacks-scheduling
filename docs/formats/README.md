# Import file formats

Stacks imports two CSV files: a **roster** (who works here) and **availability** (when each person can't, or would rather, work). The formats belong to Stacks, not to any one tool (D13). To move from Homebase, fill them in from Homebase's employee list and availability; Homebase's "Unavailable" and "Preferred" availability titles map directly to the `type` column.

Synthetic examples: [`fixtures/imports/roster.csv`](../../fixtures/imports/roster.csv) and [`fixtures/imports/availability.csv`](../../fixtures/imports/availability.csv).

Both files:

- Are UTF-8 CSV with a header row. Column names aren't case-sensitive, and their order doesn't matter. Files saved by Excel (including Windows encoding and the byte-order mark) are accepted.
- Can have up to 5,000 rows and 2 MB.
- Are previewed before anything changes. A file with any problem is rejected as a whole, with each problem listed by row and column. An applied import can be undone while it's the most recent one of its kind.

## Roster

One row per employee. People are matched to existing records by email address (case doesn't matter).

| Column | Required | Meaning |
|---|---|---|
| `full_name` | Yes | Legal or full name, shown to supervisors. |
| `preferred_name` | Yes | Shown to co-workers on the published schedule. |
| `email` | Yes | Used to match records and to send the sign-in invitation. Must be unique in the file. |
| `campus_id` | No | Must be unique across employees. |
| `employment_type` | No | `regular` (default), `non_regular` (never auto-scheduled) or `inactive`. |
| `hire_date` | No | `YYYY-MM-DD`. |
| `new_employee_until` | No | `YYYY-MM-DD`. New-employee rules apply up to this date. |
| `default_pay_role` | No | Pay role name. A name that doesn't exist yet is created. |
| `tags` | No | Tag names separated by semicolons, for example `Shift lead;Cataloguer`. Tags that don't exist yet are created. |

For people already in Stacks, a blank optional cell leaves the current value alone, and tags are only ever added, never removed. Wages aren't imported; supervisors enter them on each employee's record.

## Availability

One row per block, for the term chosen when uploading. Every person in the file gets exactly the blocks listed, replacing their availability for that term, and is marked **needs review** until they or a supervisor confirm it (D11). People not in the file are left alone. Everyone in the file must already be on the roster.

| Column | Required | Meaning |
|---|---|---|
| `email` | Yes | The employee's email address. |
| `weekday` | Yes | `Monday` to `Sunday`, or `Mon` to `Sun`. |
| `type` | Yes | `unavailable` or `preferred`. |
| `start` | No | Start time, such as `09:00`, `9:00am` or `13:30`. |
| `end` | No | End time. Leave both `start` and `end` blank for all day. |

Times are kept exactly as given, even off the 15-minute grid (for example a class ending at 9:50). For scheduling, unavailability is widened to whole 15-minute slots, so a 9:50 end blocks until 10:00 (SPEC §3.3). Preferred time that overlaps unavailable time counts as unavailable.
