# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic availability patterns shaped like a real student roster (CLAUDE.md §2.1).

Class meeting patterns are invented: Monday/Wednesday/Friday 50-minute classes and
Tuesday/Thursday 75-minute classes at typical start times, plus evening labs and
some weekends off. Seeded, so tests and the demo are reproducible.
"""

from __future__ import annotations

import random

from domain.availability import AvailabilityBlock, BlockKind, normalise_blocks
from domain.calendar import FRI, MON, SAT, SUN, THU, TUE, WED
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
