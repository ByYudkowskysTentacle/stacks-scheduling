# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from django import forms
from django.core.exceptions import ValidationError

from app.core.models import (
    Closure,
    Department,
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


class DepartmentForm(forms.ModelForm):
    open_shifts_from_choices = forms.ModelMultipleChoiceField(
        queryset=Department.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Also show this department's people open shifts from",
        help_text="They see only those departments' open shifts, not their schedules, and a "
        "supervisor of that department still approves any claim (D108).",
    )

    class Meta:
        model = Department
        fields = ["name", "coverage_default", "coverage_mode"]
        help_texts = {"name": "For example Circulation or Special Collections."}

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization
        others = Department.objects.filter(organization=organization).exclude(pk=self.instance.pk)
        field = self.fields["open_shifts_from_choices"]
        if not others.exists():
            del self.fields["open_shifts_from_choices"]
            return
        field.queryset = others
        field.initial = list(others.filter(pk__in=self.instance.open_shifts_from or []))

    def save(self, commit: bool = True) -> Department:
        if "open_shifts_from_choices" in self.fields:
            chosen = self.cleaned_data.get("open_shifts_from_choices") or []
            self.instance.open_shifts_from = sorted(d.pk for d in chosen)
        return super().save(commit)

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        clash = Department.objects.filter(organization=self.organization, name__iexact=name)
        if clash.exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("There's already a department with that name.")
        return name


class DepartmentRemoveForm(forms.Form):
    """Confirms removing a department, and where what it holds goes (D110)."""

    into = forms.ModelChoiceField(
        queryset=Department.objects.none(),
        required=False,
        label="Move its schedules, people, supervisors and rules into",
    )
    confirm = forms.BooleanField(label="I understand this department will be removed.")

    def __init__(self, *args, department: Department, needs_target: bool, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["into"].queryset = Department.objects.filter(
            organization=department.organization
        ).exclude(pk=department.pk)
        if needs_target:
            self.fields["into"].required = True
        else:
            del self.fields["into"]


class TermForm(forms.ModelForm):
    class Meta:
        model = Term
        fields = [
            "name",
            "start_date",
            "end_date",
            "availability_deadline",
            "availability_reminders",
            "reminder_days",
        ]
        widgets = {"start_date": DateInput(), "end_date": DateInput(), "availability_deadline": DateInput()}

    reminder_days = forms.CharField(
        label="Reminder days",
        required=False,
        help_text="Days before the deadline to send a reminder, separated by commas, for example 7, 3, 1. "
        "One also goes out on the deadline day.",
    )

    MAX_REMINDERS = 5

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and not self.is_bound:
            self.initial["reminder_days"] = ", ".join(str(d) for d in self.instance.reminder_days)
        elif not self.is_bound:
            self.initial.setdefault("reminder_days", "3")

    def clean_reminder_days(self) -> list[int]:
        raw = self.cleaned_data.get("reminder_days") or ""
        try:
            days = sorted(
                {int(part) for part in raw.replace(";", ",").split(",") if part.strip()}, reverse=True
            )
        except ValueError as exc:
            raise forms.ValidationError(
                "Use whole numbers separated by commas, for example 7, 3, 1."
            ) from exc
        if any(d < 1 or d > 60 for d in days):
            raise forms.ValidationError("Each day must be between 1 and 60 days before the deadline.")
        if len(days) > self.MAX_REMINDERS:
            raise forms.ValidationError(f"Choose at most {self.MAX_REMINDERS} reminder days.")
        return days


class RetentionPolicyForm(forms.ModelForm):
    class Meta:
        model = RetentionPolicy
        fields = [
            "availability_days",
            "time_off_days",
            "import_days",
            "notification_days",
            "solve_run_days",
            "audit_days",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from app.core.retention import SUGGESTED

        for name, days in SUGGESTED.items():
            field = self.fields[name]
            field.help_text = f"{field.help_text} Suggested: {days}.".strip()
