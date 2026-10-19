# Stacks (working title)

Open-source, self-hosted employee scheduling for academic libraries that run on student workers.

It covers what general-purpose tools like Homebase do (availability, shift scheduling, drafts and publishing, hours tracking) and adds what libraries need: skill-based coverage rules, per-student hour caps and work-study funding limits, an optimization solver that builds and rebalances schedules with small, explained changes, and Paycom integration.

## Status

**Milestones 1 to 5 done** (foundations, availability, manual scheduling, the scheduling engine and engine v2: suggestion queries, targeted moves, split shifts and remove-from-pool). The student schedule view and the Homebase fallback export complete the spring 2027 scope. Most of milestone 6 (self-service) is in too: a private calendar feed, time-off requests, and claiming open shifts or offering shifts for cover. Hours and coverage reports, and single-person records export and erasure, are also in. Milestone 7 (Paycom and funding) waits on API access and file formats (`docs/decisions.md`, open items). See [`docs/install.md`](docs/install.md) to install it. Python, Django, PostgreSQL and OR-Tools CP-SAT, deployed with `docker compose`. The first release targets the spring 2027 term at a pilot library. See `CLAUDE.md` §8 for how to run it.

## Documents

| Document | What it holds |
|---|---|
| [`docs/SPEC.md`](docs/SPEC.md) | Product and domain spec: the source of truth for behaviour, rules and acceptance scenarios. |
| [`docs/decisions.md`](docs/decisions.md) | Running log of maintainer decisions (D1 onwards), spec interpretations and open items. |
| [`docs/adr/0001-stack.md`](docs/adr/0001-stack.md) | Accepted architecture decision record for the technology stack, with solver benchmark results. |
| [`spikes/cpsat-benchmark/`](spikes/cpsat-benchmark/) | Throwaway solver benchmark behind the ADR (synthetic data only). |
| [`docs/install.md`](docs/install.md) | Installing on a server, backups and restores, upgrades, and the privacy sign-off. |
| [`docs/formats/README.md`](docs/formats/README.md) | The roster and availability CSV import formats. |
| [`CLAUDE.md`](CLAUDE.md) | Working guidance for contributors and AI coding sessions. |

## Privacy

Student employee records (availability, hours, wages, work-study awards) are treated as protected education records. The repository contains **synthetic data only**. Never commit real student data, exports or credentials.

## License

This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version. See [`LICENSE`](LICENSE).

SPDX-License-Identifier: `AGPL-3.0-or-later`
