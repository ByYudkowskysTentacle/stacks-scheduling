"""Synthetic roster generator for the solver benchmark spike.

Every name and value here is invented (CLAUDE.md §2.1). The shape follows the
pilot described in SPEC §3.1 and the acceptance scenarios in SPEC §12:
28 regular students, 4 non-regular, one location, a two-week planning period
that starts on Saturday (D27), and exactly 2 people on duty per open slot.
"""

from __future__ import annotations

import copy
import random
from dataclasses import dataclass, field

SLOT_MINUTES = 15
SLOTS_PER_HOUR = 60 // SLOT_MINUTES
SLOTS_PER_DAY = 24 * SLOTS_PER_HOUR

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)
WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEK_START = SAT
PERIOD_DAYS = 14


def slot(h: int, m: int = 0) -> int:
    return (h * 60 + m) // SLOT_MINUTES


def weekday_of(day: int) -> int:
    return (WEEK_START + day) % 7


def week_of(day: int) -> int:
    return day // 7


def hours(slots: int) -> float:
    return slots / SLOTS_PER_HOUR


# SPEC §3.1 pilot operating hours with the Wednesday 10-11am Chapel closure
# already cut out. Weekly open time is 73.5 hours.
OPERATING_INTERVALS: dict[int, list[tuple[int, int]]] = {
    MON: [(slot(7, 30), slot(22))],
    TUE: [(slot(7, 30), slot(22))],
    WED: [(slot(7, 30), slot(10)), (slot(11), slot(22))],
    THU: [(slot(7, 30), slot(22))],
    FRI: [(slot(7, 30), slot(16))],
    SAT: [(slot(12), slot(16))],
    SUN: [(slot(18), slot(22))],
}


def open_slots(weekday: int) -> list[int]:
    return [t for a, b in OPERATING_INTERVALS[weekday] for t in range(a, b)]


def opening_slot(weekday: int) -> int:
    return OPERATING_INTERVALS[weekday][0][0]


@dataclass
class Employee:
    id: str
    employment_type: str = "regular"  # regular | non_regular
    availability_status: str = "entered"  # entered | not_entered
    tags: frozenset[str] = frozenset()
    # weekday -> merged [start, end) slot ranges
    unavailable: dict[int, list[tuple[int, int]]] = field(default_factory=dict)
    preferred: dict[int, list[tuple[int, int]]] = field(default_factory=dict)
    weekly_cap: float = 20.0
    period_cap: float | None = None
    target_week_hours: float | None = None
    target_priority: int = 1
    rotation_group: str | None = None  # "A" works week 0, "B" works week 1


@dataclass
class Duties:
    # Scenario 3: opener starts at opening time every weekday; on the listed
    # weekdays the shift is 1.5 hours (a duty exception to the 2-hour minimum).
    opener: str | None = None
    opener_short_weekdays: tuple[int, ...] = (MON, WED, FRI)
    opener_short_slots: int = 6
    # Scenario 4: (employee, lead_week). Exactly 20h in the lead week, <=10h in the other.
    leads: tuple[tuple[str, int], ...] = ()
    lead_week_hours: float = 20.0
    lead_off_week_max: float = 10.0


@dataclass
class Rules:
    min_len: int = 2 * SLOTS_PER_HOUR
    max_len: int = 4 * SLOTS_PER_HOUR
    max_day_slots: int = 6 * SLOTS_PER_HOUR
    max_shifts_per_day: int = 2
    min_gap: int = 2  # 30 minutes
    coverage_target: int = 2
    coverage_mode: str = "exact"
    rotation: str = "none"  # none | strict | preferred
    tag_rules: bool = True


@dataclass
class Roster:
    seed: int
    employees: list[Employee]
    duties: Duties
    rules: Rules

    def get(self, emp_id: str) -> Employee:
        return next(e for e in self.employees if e.id == emp_id)

    def clone(self) -> "Roster":
        return copy.deepcopy(self)


MWF_STARTS = [(8, 0), (9, 0), (10, 0), (11, 0), (12, 0), (13, 0), (14, 0), (15, 0)]
TTH_STARTS = [(8, 0), (9, 30), (11, 0), (12, 30), (14, 0), (15, 30)]


def _block(avail: dict, weekday: int, start_min: int, end_min: int) -> None:
    # Off-grid times round toward unavailability (SPEC §3.3): an 8:50 end blocks until 9:00.
    avail.setdefault(weekday, []).append(
        (start_min // SLOT_MINUTES, -(-end_min // SLOT_MINUTES))
    )


def _merge(avail: dict) -> dict:
    out = {}
    for wd, ranges in avail.items():
        merged: list[list[int]] = []
        for a, b in sorted(ranges):
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        out[wd] = [(a, b) for a, b in merged]
    return out


def _random_unavailability(rng: random.Random, n_courses: int, avoid_mornings: bool = False) -> dict:
    un: dict = {}
    for _ in range(n_courses):
        r = rng.random()
        if r < 0.45:
            h, m = rng.choice(MWF_STARTS[1:] if avoid_mornings else MWF_STARTS)
            for wd in (MON, WED, FRI):
                _block(un, wd, h * 60 + m, h * 60 + m + 50)
        elif r < 0.9:
            h, m = rng.choice(TTH_STARTS[1:] if avoid_mornings else TTH_STARTS)
            for wd in (TUE, THU):
                _block(un, wd, h * 60 + m, h * 60 + m + 75)
        else:
            wd = rng.choice([MON, TUE, WED, THU])
            _block(un, wd, 18 * 60, 20 * 60 + 45)
    if rng.random() < 0.3:
        _block(un, SAT, 0, 24 * 60)
    if rng.random() < 0.4:
        _block(un, SUN, 0, 24 * 60)
    if rng.random() < 0.2:
        for wd in rng.choice([(MON, WED), (TUE, THU)]):
            _block(un, wd, 17 * 60, 24 * 60)
    if rng.random() < 0.15:
        _block(un, FRI, 12 * 60, 24 * 60)
    if rng.random() < 0.35:  # not a morning person
        for wd in (MON, TUE, WED, THU, FRI):
            _block(un, wd, 0, 9 * 60)
    return _merge(un)


def _random_preferred(rng: random.Random) -> dict:
    pref: dict = {}
    for _ in range(rng.randint(0, 2)):
        wd = rng.choice([MON, TUE, WED, THU, FRI])
        start = rng.choice(range(8 * 60, 19 * 60, 30))
        _block(pref, wd, start, start + 180)
    return _merge(pref)


def build_roster(seed: int, n_regular: int = 28, n_non_regular: int = 4) -> Roster:
    """Build a synthetic roster with the special people the §12 scenarios need.

    S01 opener (scenario 3), S02 and S03 leads on different weeks (scenario 4),
    S04 has a priority hour target when a scenario adds it (scenario 6),
    S05-S08 new employees (scenario 5), S09 has not_entered availability (scenario 8),
    S10 has a 10-hour weekly cap. N01-N04 are non_regular (scenario 7).
    """
    rng = random.Random(seed)
    employees: list[Employee] = []
    ids = [f"S{i:02d}" for i in range(1, n_regular + 1)]
    general = ids[10:]
    lead_tagged = {"S02", "S03", *rng.sample(general, 5)}
    cataloguers = set(rng.sample(ids[3:], 5))
    processors = set(rng.sample(ids[3:], 3))

    for i, emp_id in enumerate(ids):
        tags = set()
        if emp_id in lead_tagged:
            tags.add("Shift lead")
        if emp_id in cataloguers:
            tags.add("Cataloguer")
        if emp_id in processors:
            tags.add("Processor")
        e = Employee(id=emp_id)

        if emp_id == "S01":
            tags.add("Opener")
            un: dict = {}
            for wd in (MON, WED, FRI):  # 9am class makes the MWF opener shift 1.5h
                _block(un, wd, 9 * 60, 9 * 60 + 50)
            for wd in (TUE, THU):
                _block(un, wd, 12 * 60 + 30, 13 * 60 + 45)
            _block(un, SAT, 0, 24 * 60)
            _block(un, SUN, 0, 24 * 60)
            e.unavailable = _merge(un)
        elif emp_id in ("S02", "S03"):
            e.unavailable = _random_unavailability(rng, rng.randint(2, 3))
        elif emp_id == "S09":
            e.availability_status = "not_entered"
        else:
            e.unavailable = _random_unavailability(rng, rng.randint(3, 5))
            e.preferred = _random_preferred(rng)

        if emp_id in ("S05", "S06", "S07", "S08"):
            tags.add("New employee")
            e.period_cap = 12.0
            if emp_id in ("S05", "S06"):
                e.weekly_cap = 10.0
        if emp_id == "S10":
            e.weekly_cap = 10.0
        if emp_id not in ("S01", "S02", "S03"):
            e.rotation_group = "A" if i % 2 == 0 else "B"
        e.tags = frozenset(tags)
        employees.append(e)

    for j in range(1, n_non_regular + 1):
        employees.append(
            Employee(
                id=f"N{j:02d}",
                employment_type="non_regular",
                unavailable=_random_unavailability(rng, 3),
            )
        )

    duties = Duties(opener="S01", leads=(("S02", 0), ("S03", 1)))
    return Roster(seed=seed, employees=employees, duties=duties, rules=Rules())
