# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from django import forms
from django.core.exceptions import ValidationError

from app.core.models import (
    Closure,
    Location,
    OpenInterval,
    OperatingHoursSet,
    Organization,
    RetentionPolicy,
    Term,
)
from domain import calendar as cal
from domain.timeutil import Interval, format_minute, is_on_slot
from domain.timeutil import parse_clock as parse_time_input
from domain.timeutil import parse_ranges as parse_day_intervals


class MinuteField(forms.Field):
    """A time of day as minutes since midnight. Text input so `24:00` is possible."""

    def __init__(self, *, on_slot: bool = True, **kwargs):
        self.on_slot = on_slot
        kwargs.setdefault(
            "widget",
            forms.TextInput(
                attrs={"placeholder": "HH:MM", "inputmode": "text", "size": 7, "autocomplete": "off"}
            ),
        )
        super().__init__(**kwargs)

    def prepare_value(self, value):
        if isinstance(value, int):
            return format_minute(value)
        return value

    def to_python(self, value):
        if isinstance(value, str):
            value = value.strip()
        if value in self.empty_values:
            return None
        if isinstance(value, int):
            return value
        try:
            minute = parse_time_input(value)
        except ValueError as exc:
            raise ValidationError("Enter a time such as 07:30 or 7:30pm.") from exc
        if self.on_slot and not is_on_slot(minute):
            raise ValidationError("Use a 15-minute boundary (:00, :15, :30 or :45).")
        return minute


class OrganizationForm(forms.ModelForm):
    class Meta:
        model = Organization
        fields = ["name", "time_zone", "week_start", "planning_period_weeks"]


class LocationForm(forms.ModelForm):
    class Meta:
        model = Location
        fields = ["name", "is_active"]


class DateInput(forms.DateInput):
    input_type = "date"


class OperatingHoursForm(forms.ModelForm):
    """One text box per weekday ("07:30-22:00, ..."), easier on a phone than 14 time pickers."""

    class Meta:
        model = OperatingHoursSet
        fields = ["name", "effective_from", "effective_to"]
        widgets = {"effective_from": DateInput(), "effective_to": DateInput()}

    def __init__(self, *args, week_start: int = cal.SAT, **kwargs):
        super().__init__(*args, **kwargs)
        self.weekday_order = cal.ordered_weekdays(week_start)
        existing: dict[int, list[Interval]] = {}
        if self.instance.pk:
            for iv in self.instance.intervals.all():
                existing.setdefault(iv.weekday, []).append(Interval(iv.start_min, iv.end_min))
        for wd in self.weekday_order:
            self.fields[f"day_{wd}"] = forms.CharField(
                label=cal.WEEKDAY_NAMES[wd],
                required=False,
                help_text="Blank means closed.",
                initial=", ".join(
                    f"{format_minute(i.start)}-{format_minute(i.end)}" for i in existing.get(wd, [])
                ),
                widget=forms.TextInput(attrs={"placeholder": "07:30-22:00", "autocomplete": "off"}),
            )

    def day_fields(self):
        return [self[f"day_{wd}"] for wd in self.weekday_order]

    def clean(self):
        cleaned = super().clean()
        self.parsed: dict[int, list[Interval]] = {}
        for wd in self.weekday_order:
            try:
                self.parsed[wd] = parse_day_intervals(cleaned.get(f"day_{wd}") or "")
            except ValueError as exc:
                self.add_error(f"day_{wd}", str(exc).capitalize() + ".")
        return cleaned

    def save(self, commit: bool = True):
        hours = super().save(commit=commit)
        if commit:
            hours.intervals.all().delete()
            OpenInterval.objects.bulk_create(
                OpenInterval(
                    organization=hours.organization,
                    hours_set=hours,
                    weekday=wd,
                    start_min=iv.start,
                    end_min=iv.end,
                )
                for wd, ivs in self.parsed.items()
                for iv in ivs
            )
        return hours


class ClosureForm(forms.ModelForm):
    start_min = MinuteField(label="From", required=False, help_text="Leave both times blank for all day.")
    end_min = MinuteField(label="Until", required=False)

    class Meta:
        model = Closure
        fields = ["reason", "kind", "weekday", "on_date", "valid_from", "valid_to", "start_min", "end_min"]
        widgets = {
            "kind": forms.RadioSelect,
            "on_date": DateInput(),
            "valid_from": DateInput(),
            "valid_to": DateInput(),
        }
        help_texts = {
            "weekday": "Recurring closures only.",
            "on_date": "Dated closures only.",
            "valid_from": "Optional, recurring closures only (for example, the term start).",
        }

    def __init__(self, *args, location: Location | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        if location is not None:
            self.instance.location = location
            self.instance.organization = location.organization

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("kind") == Closure.RECURRING:
            cleaned["on_date"] = None
        elif cleaned.get("kind") == Closure.DATED:
            cleaned["weekday"] = cleaned["valid_from"] = cleaned["valid_to"] = None
        return cleaned


class TermForm(forms.ModelForm):
    class Meta:
        model = Term
        fields = ["name", "start_date", "end_date"]
        widgets = {"start_date": DateInput(), "end_date": DateInput()}


class RetentionPolicyForm(forms.ModelForm):
    class Meta:
        model = RetentionPolicy
        fields = ["availability_days", "time_off_days", "import_days", "notification_days", "solve_run_days"]
