# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from decimal import Decimal

from django import forms

from app.core.forms import DateInput, MinuteField
from app.people.models import Employee, PayRole, Tag
from app.scheduling.models import (
    CoverageOverride,
    HourCapRule,
    SchedulePeriod,
    SchedulingRules,
    Shift,
    TagCoverageRule,
)
from domain import calendar as cal
from domain import rules as dom

WEEKDAY_CHOICES = [(i, n) for i, n in enumerate(cal.WEEKDAY_NAMES)]


class HoursField(forms.DecimalField):
    """Hours in the form, minutes in the database."""

    def __init__(self, **kwargs):
        kwargs.setdefault("max_digits", 5)
        kwargs.setdefault("decimal_places", 2)
        kwargs.setdefault("min_value", Decimal("0"))
        super().__init__(**kwargs)

    def prepare_value(self, value):
        if isinstance(value, int):
            return f"{value / 60:g}"
        return value

    def clean(self, value):
        hours = super().clean(value)
        return None if hours is None else round(hours * 60)


def ordered_weekday_choices(week_start: int):
    return [(wd, cal.WEEKDAY_NAMES[wd]) for wd in cal.ordered_weekdays(week_start)]


class PeriodForm(forms.ModelForm):
    class Meta:
        model = SchedulePeriod
        fields = ["start_date", "weeks"]
        widgets = {"start_date": DateInput()}
        help_texts = {"start_date": "Moved back to the start of its week if needed."}

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization
        self.fields["weeks"].initial = organization.planning_period_weeks

    def clean_start_date(self):
        return cal.week_start_for(self.cleaned_data["start_date"], self.organization.week_start)


class ShiftForm(forms.ModelForm):
    start_min = MinuteField(label="Start", help_text="For example 9:00 or 1:30pm.")
    end_min = MinuteField(label="End")

    class Meta:
        model = Shift
        fields = ["employee", "date", "start_min", "end_min", "pay_role", "note"]
        widgets = {"date": DateInput()}
        help_texts = {"note": "Optional and visible to the employee. No personal details."}

    def __init__(self, *args, period: SchedulePeriod, **kwargs):
        super().__init__(*args, **kwargs)
        org = period.organization
        self.instance.period = period
        self.instance.organization = org
        self.instance.location = period.location
        employees = Employee.objects.filter(organization=org).exclude(employment_type="inactive")
        if self.instance.employee_id:
            employees = employees.filter(archived_at__isnull=True) | Employee.objects.filter(
                pk=self.instance.employee_id
            )
        else:
            employees = employees.filter(archived_at__isnull=True)
        self.fields["employee"].queryset = employees.order_by("preferred_name")
        self.fields["employee"].empty_label = "Open shift (no one assigned)"
        self.fields["employee"].required = False
        self.fields["pay_role"].queryset = PayRole.objects.filter(organization=org, is_active=True)
        self.fields["pay_role"].required = False
        self.fields["pay_role"].help_text = "Leave blank to use the employee's default pay role."
        self.fields["date"].widget.attrs.update(
            min=period.start_date.isoformat(), max=period.end_date.isoformat()
        )

    def clean(self):
        cleaned = super().clean()
        employee = cleaned.get("employee")
        old_employee = self.instance.employee if self.instance.pk else None
        pay_role = cleaned.get("pay_role")
        # Scenario 15: reassigning a shift moves it to the new person's default pay role unless the
        # supervisor chose a different one on purpose.
        if employee is not None and (
            pay_role is None
            or (
                old_employee is not None
                and old_employee != employee
                and pay_role == old_employee.default_pay_role
            )
        ):
            cleaned["pay_role"] = employee.default_pay_role
        return cleaned


class CallOutForm(forms.Form):
    covered_by = forms.ModelChoiceField(
        queryset=Employee.objects.none(),
        required=False,
        empty_label="No one yet (leave it as an open shift)",
        help_text="Who's working the shift instead.",
    )
    note = forms.CharField(
        required=False, max_length=200, help_text="Optional. No medical or personal details."
    )

    def __init__(self, *args, shift: Shift, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["covered_by"].queryset = (
            Employee.objects.current().filter(organization=shift.organization).exclude(pk=shift.employee_id)
        )


class SchedulingRulesForm(forms.ModelForm):
    min_shift_minutes = HoursField(label="Minimum shift (hours)")
    max_shift_minutes = HoursField(label="Maximum shift (hours)")
    max_daily_minutes = HoursField(label="Maximum hours per person per day")
    min_gap_minutes = forms.IntegerField(label="Minimum gap between a person's shifts (minutes)", min_value=0)
    default_weekly_cap_minutes = HoursField(
        label="Default weekly cap (hours)", required=False, help_text="Blank means none."
    )

    class Meta:
        model = SchedulingRules
        fields = [
            "coverage_default",
            "coverage_mode",
            "min_shift_minutes",
            "max_shift_minutes",
            "max_daily_minutes",
            "max_shifts_per_day",
            "min_gap_minutes",
            "default_weekly_cap_minutes",
            "freeze_window_hours",
            "change_limit_percent",
        ]

    def clean(self):
        cleaned = super().clean()
        lo, hi = cleaned.get("min_shift_minutes"), cleaned.get("max_shift_minutes")
        if lo is not None and hi is not None and lo > hi:
            self.add_error("max_shift_minutes", "The maximum is shorter than the minimum.")
        return cleaned


class CoverageOverrideForm(forms.ModelForm):
    weekdays = forms.TypedMultipleChoiceField(coerce=int, widget=forms.CheckboxSelectMultiple)
    start_min = MinuteField(label="From")
    end_min = MinuteField(label="Until")

    class Meta:
        model = CoverageOverride
        fields = ["weekdays", "start_min", "end_min", "count"]

    def __init__(self, *args, week_start: int, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["weekdays"].choices = ordered_weekday_choices(week_start)

    def clean(self):
        cleaned = super().clean()
        s, e = cleaned.get("start_min"), cleaned.get("end_min")
        if s is not None and e is not None and s >= e:
            self.add_error("end_min", "Ends before it starts.")
        return cleaned


class HourCapForm(forms.ModelForm):
    minutes = HoursField(label="Hours")

    class Meta:
        model = HourCapRule
        fields = ["scope", "minutes", "employee", "tag", "label"]
        help_texts = {
            "employee": "Pick an employee, or a tag, or neither to cap everyone.",
            "label": 'Optional, for example "New employees\' first period".',
        }

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["employee"].queryset = Employee.objects.current().filter(organization=organization)
        self.fields["tag"].queryset = Tag.objects.filter(organization=organization)

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("employee") and cleaned.get("tag"):
            raise forms.ValidationError("Choose an employee or a tag, not both.")
        return cleaned


class TagRuleForm(forms.ModelForm):
    weekdays = forms.TypedMultipleChoiceField(
        coerce=int,
        widget=forms.CheckboxSelectMultiple,
        required=False,
        help_text="None ticked means every day.",
    )
    minutes = HoursField(label="Hours per week", required=False, help_text="For weekly-hours rules only.")
    start_min = MinuteField(
        label="From", required=False, help_text="Leave both times blank for all open hours."
    )
    end_min = MinuteField(label="Until", required=False)

    class Meta:
        model = TagCoverageRule
        fields = ["tag", "kind", "hard", "count", "minutes", "weekdays", "start_min", "end_min", "label"]

    def __init__(self, *args, organization, week_start: int, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["tag"].queryset = Tag.objects.filter(organization=organization)
        self.fields["weekdays"].choices = ordered_weekday_choices(week_start)

    def clean(self):
        cleaned = super().clean()
        cleaned["minutes"] = cleaned.get("minutes") or 0
        s, e = cleaned.get("start_min"), cleaned.get("end_min")
        if (s is None) != (e is None):
            self.add_error("end_min", "Give both times, or neither.")
        elif s is not None and e is not None and s >= e:
            self.add_error("end_min", "Ends before it starts.")
        if cleaned.get("kind") == dom.TagRuleKind.MIN_WEEKLY_HOURS and not cleaned["minutes"]:
            self.add_error("minutes", "Give the weekly hours.")
        return cleaned
