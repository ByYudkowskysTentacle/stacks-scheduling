# SPDX-License-Identifier: AGPL-3.0-or-later
"""Weekly availability entry: two text boxes per weekday (unavailable, preferred).

The text boxes are the accessible, no-JavaScript way to enter availability and the
source of truth for the form. The paintable grid (static/js/availability-grid.js)
only reads and writes these boxes.
"""

from __future__ import annotations

from django import forms

from app.core.forms import MinuteField
from domain import calendar as cal
from domain.availability import AvailabilityBlock, BlockKind, WeeklyAvailability, normalise_blocks
from domain.timeutil import DAY_MINUTES, Interval, format_ranges_12h, parse_ranges

ALL_DAY_WORDS = {"all day", "all-day", "allday", "all"}


def parse_block_text(text: str) -> list[Interval]:
    if text.strip().lower() in ALL_DAY_WORDS:
        return [Interval(0, DAY_MINUTES)]
    return parse_ranges(text, on_slot=False)


def field_name(kind: BlockKind, weekday: int) -> str:
    return f"{kind.value}_{weekday}"


class WeeklyAvailabilityForm(forms.Form):
    def __init__(
        self, *args, week_start: int, initial_availability: WeeklyAvailability | None = None, **kwargs
    ):
        super().__init__(*args, **kwargs)
        self.weekday_order = cal.ordered_weekdays(week_start)
        for wd in self.weekday_order:
            for kind, label, placeholder in (
                (BlockKind.UNAVAILABLE, "Unavailable", "e.g. 09:00-09:50"),
                (BlockKind.PREFERRED, "Preferred", "e.g. 13:00-17:00"),
            ):
                initial = ""
                if initial_availability is not None:
                    exact = initial_availability.exact(wd, kind)
                    initial = "all day" if exact == [Interval(0, DAY_MINUTES)] else format_ranges_12h(exact)
                self.fields[field_name(kind, wd)] = forms.CharField(
                    label=f"{cal.WEEKDAY_NAMES[wd]}: {label.lower()}",
                    required=False,
                    initial=initial,
                    widget=forms.TextInput(
                        attrs={
                            "placeholder": placeholder,
                            "autocomplete": "off",
                            "data-weekday": wd,
                            "data-kind": kind.value,
                        }
                    ),
                )

    def rows(self):
        """(weekday name, unavailable field, preferred field) in week order, for the template."""
        return [
            (
                cal.WEEKDAY_NAMES[wd],
                self[field_name(BlockKind.UNAVAILABLE, wd)],
                self[field_name(BlockKind.PREFERRED, wd)],
            )
            for wd in self.weekday_order
        ]

    def clean(self):
        cleaned = super().clean()
        blocks: list[AvailabilityBlock] = []
        for wd in self.weekday_order:
            for kind in BlockKind:
                name = field_name(kind, wd)
                try:
                    blocks += [
                        AvailabilityBlock(wd, kind, iv) for iv in parse_block_text(cleaned.get(name) or "")
                    ]
                except ValueError as exc:
                    self.add_error(
                        name, f"{str(exc).capitalize()}. Use ranges like 09:00-09:50, or 'all day'."
                    )
        self.blocks = normalise_blocks(blocks)
        return cleaned


class TimeOffForm(forms.Form):
    """A time-off request. No reason is asked for (SPEC §11)."""

    start_date = forms.DateField(label="First day", widget=forms.DateInput(attrs={"type": "date"}))
    end_date = forms.DateField(
        label="Last day",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="Leave blank for one day.",
    )
    all_day = forms.BooleanField(label="All day", required=False, initial=True)
    start_min = MinuteField(label="Start time", required=False, help_text="For example 9am or 1:30pm.")
    end_min = MinuteField(label="End time", required=False)

    MAX_DAYS = 60

    def __init__(self, *args, today, **kwargs):
        super().__init__(*args, **kwargs)
        self.today = today

    def clean(self):
        data = super().clean()
        start, end = data.get("start_date"), data.get("end_date") or data.get("start_date")
        if start is None:
            return data
        if start < self.today:
            self.add_error("start_date", "Choose today or a later date.")
        if end < start:
            self.add_error("end_date", "The end date is before the start date.")
        elif (end - start).days >= self.MAX_DAYS:
            self.add_error("end_date", f"Ask for at most {self.MAX_DAYS} days at a time.")
        data["end_date"] = end
        if data.get("all_day"):
            data["start_min"] = data["end_min"] = None
        elif data.get("start_min") is None or data.get("end_min") is None:
            self.add_error("start_min", "Give both times, or tick all day.")
        elif data["start_min"] >= data["end_min"]:
            self.add_error("end_min", "The end time is before the start time.")
        return data
