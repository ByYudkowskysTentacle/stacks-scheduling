# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic availability patterns shaped like a real student roster (CLAUDE.md §2.1).

Class meeting patterns are invented: Monday/Wednesday/Friday 50-minute classes and
Tuesday/Thursday 75-minute classes at typical start times, plus evening labs and
some weekends off. Seeded, so tests and the demo are reproducible.
"""

from __future__ import annotations

import random
from datetime import date

from domain.availability import (
    AvailabilityBlock,
    AvailabilityStatus,
    BlockKind,
    WeeklyAvailability,
    normalise_blocks,
)
from domain.calendar import FRI, MON, SAT, SUN, THU, TUE, WED, Closure, OperatingHours
from domain.rules import (
    CapScope,
    CoverageMode,
    CoverageRules,
    HourCap,
    OpenerDuty,
    Rotation,
    RotationMode,
    Rules,
    TagRule,
    TagRuleKind,
    WeekHoursDuty,
)
from domain.schedule import Period, Person, ScheduleContext
from domain.timeutil import Interval

MWF_STARTS = [480, 540, 600, 660, 720, 780, 840, 900]
TTH_STARTS = [480, 570, 660, 750, 840, 930]


def _un(weekday: int, start: int, end: int) -> AvailabilityBlock:
    return AvailabilityBlock(weekday, BlockKind.UNAVAILABLE, Interval(start, end))


def random_unavailability(
    rng: random.Random, n_courses: int, avoid_mornings: bool = False
) -> list[AvailabilityBlock]:
    blocks: list[AvailabilityBlock] = []
    for _ in range(n_courses):
        r = rng.random()
        if r < 0.45:
            start = rng.choice(MWF_STARTS[1:] if avoid_mornings else MWF_STARTS)
            blocks += [_un(wd, start, start + 50) for wd in (MON, WED, FRI)]
        elif r < 0.9:
            start = rng.choice(TTH_STARTS[1:] if avoid_mornings else TTH_STARTS)
            blocks += [_un(wd, start, start + 75) for wd in (TUE, THU)]
        else:
            blocks.append(_un(rng.choice([MON, TUE, WED, THU]), 18 * 60, 20 * 60 + 45))
    if rng.random() < 0.3:
        blocks.append(_un(SAT, 0, 1440))
    if rng.random() < 0.4:
        blocks.append(_un(SUN, 0, 1440))
    if rng.random() < 0.2:
        blocks += [_un(wd, 17 * 60, 1440) for wd in rng.choice([(MON, WED), (TUE, THU)])]
    if rng.random() < 0.15:
        blocks.append(_un(FRI, 12 * 60, 1440))
    if rng.random() < 0.35:  # not a morning person
        blocks += [_un(wd, 0, 9 * 60) for wd in (MON, TUE, WED, THU, FRI)]
    return blocks


def random_preferred(rng: random.Random) -> list[AvailabilityBlock]:
    blocks = []
    for _ in range(rng.randint(0, 2)):
        start = rng.choice(range(8 * 60, 19 * 60, 30))
        blocks.append(
            AvailabilityBlock(
                rng.choice([MON, TUE, WED, THU, FRI]), BlockKind.PREFERRED, Interval(start, start + 180)
            )
        )
    return blocks


def opener_pattern() -> list[AvailabilityBlock]:
    """Scenario 3's opener: a 9am class on Mon, Wed and Fri, so the opening shift there is 1.5 hours."""
    blocks = [_un(wd, 540, 590) for wd in (MON, WED, FRI)]
    blocks += [_un(wd, 750, 825) for wd in (TUE, THU)]
    blocks += [_un(SAT, 0, 1440), _un(SUN, 0, 1440)]
    return blocks


def student_pattern(rng: random.Random) -> list[AvailabilityBlock]:
    return normalise_blocks(random_unavailability(rng, rng.randint(3, 5)) + random_preferred(rng))


# --- A pilot-shaped roster for engine tests (SPEC §12) --------------------------------------

PILOT_START = date(2027, 1, 16)  # a Saturday
OPENER, LEAD_X, LEAD_Z, PRIORITY, NOT_ENTERED, CAPPED = 1, 2, 3, 4, 9, 10
NEW_EMPLOYEES = (5, 6, 7, 8)
NON_REGULAR = (101, 102, 103, 104)


def pilot_hours() -> list[OperatingHours]:
    return [
        OperatingHours(
            date(2026, 8, 1),
            None,
            {
                **{wd: [Interval(450, 1320)] for wd in (MON, TUE, WED, THU)},
                FRI: [Interval(450, 960)],
                SAT: [Interval(720, 960)],
                SUN: [Interval(1080, 1320)],
            },
        )
    ]


CHAPEL = Closure(reason="Chapel", weekday=WED, interval=Interval(600, 660))


def pilot_context(
    seed: int = 1, rotation: RotationMode = RotationMode.NONE, chapel: bool = True
) -> ScheduleContext:
    """28 regular students and 4 non_regular employees over a two-week, Saturday-start period,
    with the scenario-specific people: an opener (scenario 3), two leads on different weeks
    (scenario 4), four new employees (scenario 5), someone with no availability entered
    (scenario 8) and someone with a 10-hour weekly cap. Mirrors the benchmark spike's roster."""
    rng = random.Random(seed)  # noqa: S311 (synthetic data)
    ids = list(range(1, 29))
    general = ids[10:]
    lead_tagged = {LEAD_X, LEAD_Z, *rng.sample(general, 5)}
    cataloguers = set(rng.sample(ids[3:], 5))
    processors = set(rng.sample(ids[3:], 3))
    period = Period(PILOT_START, 2, SAT)
    people: dict[int, Person] = {}
    groups: dict[int, str] = {}
    for i, pid in enumerate(ids):
        tags: set[str] = set()
        if pid in lead_tagged:
            tags.add("Shift lead")
        if pid in cataloguers:
            tags.add("Cataloguer")
        if pid in processors:
            tags.add("Processor")
        status = AvailabilityStatus.ENTERED
        if pid == OPENER:
            tags.add("Opener")
            blocks = opener_pattern()
        elif pid in (LEAD_X, LEAD_Z):
            blocks = normalise_blocks(random_unavailability(rng, rng.randint(2, 3)))
        elif pid == NOT_ENTERED:
            blocks, status = [], AvailabilityStatus.NOT_ENTERED
        else:
            blocks = student_pattern(rng)
        if pid in NEW_EMPLOYEES:
            tags.add("New employee")
        if pid not in (OPENER, LEAD_X, LEAD_Z):
            groups[pid] = "A" if i % 2 == 0 else "B"
        people[pid] = Person(
            pid,
            f"Student {pid:02d}",
            WeeklyAvailability(status, tuple(blocks)),
            {d: frozenset(tags) for d in period.days},
        )
    for pid in NON_REGULAR:
        blocks = normalise_blocks(random_unavailability(rng, 3))
        people[pid] = Person(
            pid,
            f"Staff {pid}",
            WeeklyAvailability(AvailabilityStatus.ENTERED, tuple(blocks)),
            {d: frozenset() for d in period.days},
            auto_schedulable=False,
        )
    rules = Rules(
        coverage=CoverageRules(default=2, mode=CoverageMode.EXACT),
        caps=[
            HourCap(CapScope.WEEK, 20 * 60, label="default weekly cap"),
            HourCap(CapScope.PERIOD, 12 * 60, tag="New employee", label="new-employee period cap"),
            HourCap(CapScope.WEEK, 10 * 60, employee_id=5),
            HourCap(CapScope.WEEK, 10 * 60, employee_id=6),
            HourCap(CapScope.WEEK, 10 * 60, employee_id=CAPPED),
        ],
        tag_rules=[
            TagRule("Shift lead", TagRuleKind.MIN_ON_DUTY),
            TagRule(
                "Cataloguer",
                TagRuleKind.MIN_ON_DUTY,
                weekdays=frozenset({MON, TUE, WED, THU, FRI}),
                interval=Interval(540, 1020),
            ),
            TagRule("Processor", TagRuleKind.MIN_WEEKLY_HOURS, minutes=15 * 60),
            TagRule("New employee", TagRuleKind.NOT_ALONE),
        ],
        rotation=Rotation(rotation, groups),
        openers=[OpenerDuty(OPENER, frozenset({MON, TUE, WED, THU, FRI}), frozenset({MON, WED, FRI}), 90)],
        week_duties=[
            WeekHoursDuty(LEAD_X, frozenset({0}), 20 * 60, 10 * 60),
            WeekHoursDuty(LEAD_Z, frozenset({1}), 20 * 60, 10 * 60),
        ],
    )
    return ScheduleContext(period, pilot_hours(), [CHAPEL] if chapel else [], people, rules, [])
