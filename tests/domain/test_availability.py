# SPDX-License-Identifier: AGPL-3.0-or-later
from datetime import date

from hypothesis import given
from hypothesis import strategies as st

from domain.availability import (
    AvailabilityBlock,
    AvailabilityStatus,
    BlockKind,
    WeeklyAvailability,
    coverage_summary,
    normalise_blocks,
    summarise_week,
)
from domain.calendar import MON, SAT, SUN, WED
from domain.timeutil import Interval, intersect, is_on_slot, total_minutes
from tests.domain.test_calendar import CHAPEL, PILOT, SATURDAY

UN, PREF = BlockKind.UNAVAILABLE, BlockKind.PREFERRED
OPEN_MON = [Interval(450, 1320)]


def avail(*blocks, status=AvailabilityStatus.ENTERED):
    return WeeklyAvailability(status, tuple(AvailabilityBlock(*b) for b in blocks))


def test_off_grid_unavailability_rounds_toward_unavailable():
    # SPEC §3.3: an 8:50 end blocks until 9:00, and a 9:05 start blocks from 9:00.
    a = avail((MON, UN, Interval(470, 530)))  # 7:50-8:50
    assert a.unavailable_for_scheduling(MON) == [Interval(465, 540)]
    assert a.exact(MON, UN) == [Interval(470, 530)]  # kept exactly for display


def test_available_within_open_hours():
    a = avail((MON, UN, Interval(540, 590)))  # class 9:00-9:50
    assert a.available_within(MON, OPEN_MON) == [Interval(450, 540), Interval(600, 1320)]


def test_not_entered_is_never_available():
    a = WeeklyAvailability(AvailabilityStatus.NOT_ENTERED)
    assert a.available_within(MON, OPEN_MON) == []


def test_entered_with_no_blocks_is_fully_available():
    a = WeeklyAvailability(AvailabilityStatus.ENTERED)
    assert a.available_within(MON, OPEN_MON) == OPEN_MON


def test_preferred_never_overlaps_unavailable():
    a = avail((MON, PREF, Interval(480, 720)), (MON, UN, Interval(600, 660)))
    assert a.preferred_for_scheduling(MON) == [Interval(480, 600), Interval(660, 720)]


def test_normalise_merges_and_unavailable_wins():
    blocks = normalise_blocks(
        [
            AvailabilityBlock(MON, UN, Interval(540, 600)),
            AvailabilityBlock(MON, UN, Interval(590, 660)),
            AvailabilityBlock(MON, PREF, Interval(600, 720)),
        ]
    )
    assert blocks == [
        AvailabilityBlock(MON, UN, Interval(540, 660)),
        AvailabilityBlock(MON, PREF, Interval(660, 720)),
    ]


def test_week_summary_counts_available_hours_and_days_out():
    a = avail((SAT, UN, Interval(0, 1440)), (SUN, UN, Interval(0, 1440)), (WED, UN, Interval(540, 590)))
    s = summarise_week(a, SATURDAY, [PILOT], [CHAPEL])
    assert total_minutes(s.days[0].open) == 240
    assert s.days_fully_out == ["Sat", "Sun"]
    # 73.5 open hours, minus Sat (4h) and Sun (4h), minus Wednesday 9:00-9:50 rounded to 9:00-10:00.
    assert s.available_minutes == (73.5 - 4 - 4 - 1) * 60


def test_coverage_summary_counts_not_entered_separately():
    free = summarise_week(WeeklyAvailability(AvailabilityStatus.ENTERED), SATURDAY, [PILOT], [CHAPEL])
    part = summarise_week(avail((MON, UN, Interval(540, 600))), SATURDAY, [PILOT], [CHAPEL])
    unknown = summarise_week(WeeklyAvailability(AvailabilityStatus.NOT_ENTERED), SATURDAY, [PILOT], [CHAPEL])
    monday = next(c for c in coverage_summary([free, part, unknown]) if c.day == date(2027, 1, 18))
    assert (monday.free_all_open_hours, monday.free_part, monday.not_entered) == (1, 1, 1)


minutes = st.integers(min_value=0, max_value=1439)


@st.composite
def intervals(draw):
    a = draw(minutes)
    b = draw(st.integers(min_value=a + 1, max_value=1440))
    return Interval(a, b)


@given(st.lists(intervals(), max_size=6))
def test_scheduling_availability_never_touches_exact_unavailability(blocks):
    a = WeeklyAvailability(AvailabilityStatus.ENTERED, tuple(AvailabilityBlock(MON, UN, iv) for iv in blocks))
    available = a.available_within(MON, [Interval(0, 1440)])
    assert intersect(available, blocks) == []
    assert all(is_on_slot(iv.start) and is_on_slot(iv.end) for iv in available)
