# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from decimal import Decimal

from django import forms
from django.urls import reverse
from django.utils.html import format_html

from app.core.forms import DateInput, MinuteField
from app.core.models import Term
from app.people.models import Employee, PayRole, Tag
from app.scheduling.models import (
    MAX_MONTHS,
    CoverageOverride,
    DesiredHours,
    DutyAssignment,
    DutyKind,
    HourCapRule,
    HourTargetRule,
    PeriodLength,
    RotationAssignment,
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
    """A new planning period: a number of weeks, calendar months or one whole term (D120)."""

    class Meta:
        model = SchedulePeriod
        fields = ["department", "length", "start_date", "weeks", "months", "term"]
        widgets = {"start_date": DateInput(), "length": forms.RadioSelect}
        labels = {
            "length": "Length",
            "start_date": "Starts",
            "weeks": "Number of weeks",
            "months": "Number of months",
            "term": "Term",
        }
        help_texts = {
            "department": "Whose schedule this is. Each department plans its own.",
            "start_date": "For weeks, moved back to the start of its week; for months, to the 1st. "
            "A term period uses the term's dates.",
            "weeks": "1 to 4, when the length is weeks.",
            "months": f"1 to {MAX_MONTHS}, when the length is calendar months.",
            "term": "When the length is a whole term: every day from its start to its end, breaks included.",
        }

    def __init__(self, *args, organization, departments=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization
        terms = Term.objects.filter(organization=organization).order_by("-start_date")
        length = self.fields["length"]
        length.choices = PeriodLength.choices  # no blank choice
        if not terms.exists():  # nothing to pick from: say where terms come from instead
            length.choices = [c for c in PeriodLength.choices if c[0] != PeriodLength.TERM]
            length.help_text = format_html(
                'To plan a whole term, <a href="{}">add a term under Settings, Terms</a> first.',
                reverse("terms"),
            )
            del self.fields["term"]
        self.fields["length"].required = False  # weeks when left out, as before lengths existed
        self.fields["length"].initial = PeriodLength.WEEKS
        self.fields["weeks"].initial = organization.planning_period_weeks
        self.fields["months"].initial = 1
        self.fields["start_date"].required = False
        for name in ("length", "start_date", "weeks", "months", "term"):
            if name in self.fields:
                self.fields[name].hide_optional = True  # each is required for the lengths that use it
        if "term" in self.fields:
            self.fields["term"].queryset = terms
            self.fields["term"].empty_label = "Choose…"
        field = self.fields["department"]
        field.required = True
        field.empty_label = None
        field.queryset = departments if departments is not None else organization.departments.all()
        if field.queryset.count() == 1:
            field.initial = field.queryset.first()

    def clean(self):
        cleaned = super().clean()
        length = cleaned["length"] = cleaned.get("length") or PeriodLength.WEEKS
        start = cleaned.get("start_date")
        if length == PeriodLength.TERM:
            term = cleaned.get("term")
            if term is None:
                self.add_error("term", "Choose the term.")
                return cleaned
            cleaned["start_date"] = term.start_date
        elif start is None:
            self.add_error("start_date", "Choose the first day.")
            return cleaned
        elif length == PeriodLength.MONTHS:
            if not cleaned.get("months"):
                self.add_error("months", f"Enter 1 to {MAX_MONTHS} months.")
            cleaned["start_date"] = cal.month_start(start)
        elif length == PeriodLength.WEEKS:
            if not cleaned.get("weeks"):
                self.add_error("weeks", "Enter 1 to 4 weeks.")
            cleaned["start_date"] = cal.week_start_for(start, self.organization.week_start)
        # Only the chosen length's value is kept; save() clears the others too.
        for name, kind in (
            ("weeks", PeriodLength.WEEKS),
            ("months", PeriodLength.MONTHS),
            ("term", PeriodLength.TERM),
        ):
            if length != kind:
                cleaned[name] = None
        return cleaned

    def _post_clean(self):
        super()._post_clean()
        if not self.errors:
            self.instance.end_date = self.instance.planned_end()


class ShiftForm(forms.ModelForm):
    start_min = MinuteField(label="Start", help_text="For example 9am or 1:30pm.")
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
            "rotation_mode",
            "freeze_window_hours",
            "change_limit_percent",
        ]

    def clean(self):
        cleaned = super().clean()
        lo, hi = cleaned.get("min_shift_minutes"), cleaned.get("max_shift_minutes")
        if lo is not None and hi is not None and lo > hi:
            self.add_error("max_shift_minutes", "The maximum is shorter than the minimum.")
        return cleaned


def _department_field(form: forms.ModelForm, organization) -> None:
    """Which department's schedules a coverage rule is for; none means all of them (D95). With
    one department or none, the choice is hidden: the rule applies everywhere."""
    field = form.fields["department"]
    departments = organization.departments.all() if organization is not None else None
    if departments is None or departments.count() < 2:
        del form.fields["department"]
        return
    field.queryset = departments
    field.empty_label = "Every department"


class CoverageOverrideForm(forms.ModelForm):
    weekdays = forms.TypedMultipleChoiceField(coerce=int, widget=forms.CheckboxSelectMultiple)
    start_min = MinuteField(label="From")
    end_min = MinuteField(label="Until")

    class Meta:
        model = CoverageOverride
        fields = ["department", "weekdays", "start_min", "end_min", "count"]

    def __init__(self, *args, week_start: int, organization=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["weekdays"].choices = ordered_weekday_choices(week_start)
        _department_field(self, organization)

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
        fields = [
            "department",
            "tag",
            "kind",
            "hard",
            "count",
            "minutes",
            "weekdays",
            "start_min",
            "end_min",
            "label",
        ]

    def __init__(self, *args, organization, week_start: int, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["tag"].queryset = Tag.objects.filter(organization=organization)
        self.fields["weekdays"].choices = ordered_weekday_choices(week_start)
        _department_field(self, organization)

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


class RotationForm(forms.ModelForm):
    class Meta:
        model = RotationAssignment
        fields = ["employee", "group"]

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["employee"].queryset = Employee.objects.current().filter(organization=organization)


class DutyForm(forms.ModelForm):
    weekdays = forms.TypedMultipleChoiceField(
        coerce=int, widget=forms.CheckboxSelectMultiple, required=False, label="Opens on"
    )
    short_weekdays = forms.TypedMultipleChoiceField(
        coerce=int,
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label="Shorter opening shift on",
        help_text="For example the days they have a 9am class.",
    )
    short_minutes = HoursField(label="Shorter shift length (hours)", required=False)
    weeks = forms.TypedMultipleChoiceField(
        coerce=int,
        choices=[(i, f"Week {i}") for i in range(1, 5)],
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label="Their weeks",
    )
    minutes = HoursField(label="Hours in their weeks", required=False)
    off_week_max_minutes = HoursField(label="Most hours in other weeks", required=False)

    class Meta:
        model = DutyAssignment
        fields = [
            "employee",
            "kind",
            "weekdays",
            "short_weekdays",
            "short_minutes",
            "weeks",
            "minutes",
            "off_week_max_minutes",
            "label",
        ]
        help_texts = {"label": 'Shown in explanations, for example "lead week".'}

    def __init__(self, *args, organization, week_start: int, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["employee"].queryset = Employee.objects.current().filter(organization=organization)
        self.fields["weekdays"].choices = ordered_weekday_choices(week_start)
        self.fields["short_weekdays"].choices = ordered_weekday_choices(week_start)

    def clean(self):
        cleaned = super().clean()
        for name, default in (("short_minutes", 90), ("minutes", 0), ("off_week_max_minutes", 0)):
            if cleaned.get(name) is None:
                cleaned[name] = default
        if cleaned.get("kind") == DutyKind.OPENER and not cleaned.get("weekdays"):
            self.add_error("weekdays", "Choose the days they open.")
        if cleaned.get("kind") == DutyKind.WEEK_HOURS and not (
            cleaned.get("weeks") and cleaned.get("minutes")
        ):
            self.add_error("weeks", "Choose their weeks and hours.")
        return cleaned


class HourTargetForm(forms.ModelForm):
    minutes = HoursField(label="Weekly hours to aim for")

    class Meta:
        model = HourTargetRule
        fields = ["employee", "minutes", "priority", "note"]
        help_texts = {"priority": "1 to 5. Higher priorities win when targets compete. Caps always win."}

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["employee"].queryset = Employee.objects.current().filter(organization=organization)


class DesiredHoursForm(forms.Form):
    hours = forms.DecimalField(
        min_value=0,
        max_value=40,
        decimal_places=2,
        label="Hours a week you'd like to work",
        help_text="A supervisor reviews this. It doesn't change your schedule until it's approved.",
    )


def desired_initial(desired: DesiredHours | None) -> dict:
    return {"hours": f"{desired.minutes / 60:g}"} if desired else {}


def minutes_by_person(period: SchedulePeriod) -> dict[int, int]:
    """Scheduled minutes per employee in the period's working copy."""
    out: dict[int, int] = {}
    for employee_id, start, end in (
        period.shifts.live().filter(employee__isnull=False).values_list("employee_id", "start_min", "end_min")
    ):
        out[employee_id] = out.get(employee_id, 0) + end - start
    return out


def label_with_hours(minutes: dict[int, int]):
    """Choice labels such as "Riley (18h this period)", so a supervisor picking donors can see
    who has hours to spare without leaving the page (audit 2.10)."""

    def label(e: Employee) -> str:
        return f"{e.display_name} ({minutes.get(e.pk, 0) / 60:g}h this period)"

    return label


class TargetedMoveForm(forms.Form):
    """ "Move about 2 hours from D, T or P to J, keeping J in one week" (SPEC §7.3)."""

    recipient = forms.ModelChoiceField(queryset=Employee.objects.none(), label="Give hours to")
    donors = forms.ModelMultipleChoiceField(
        queryset=Employee.objects.none(), label="Take them from", widget=forms.CheckboxSelectMultiple
    )
    hours = forms.DecimalField(
        label="About how many hours", min_value=0.25, max_value=40, initial=2, decimal_places=2
    )
    week = forms.TypedChoiceField(label="Keep them in", required=False, coerce=int, empty_value=None)

    def __init__(self, *args, period: SchedulePeriod, **kwargs):
        super().__init__(*args, **kwargs)
        people = (
            Employee.objects.filter(organization=period.organization, archived_at__isnull=True)
            .exclude(employment_type="inactive")
            .order_by("preferred_name")
        )
        self.fields["recipient"].queryset = people
        self.fields["donors"].queryset = people
        label = label_with_hours(minutes_by_person(period))
        self.fields["recipient"].label_from_instance = label
        self.fields["donors"].label_from_instance = label
        self.fields["week"].choices = [("", "Any week")] + [
            (w, f"Week {w + 1} only") for w in range(period.week_count)
        ]

    def clean(self):
        data = super().clean()
        if data.get("recipient") and data.get("donors") and data["recipient"] in data["donors"]:
            self.add_error("donors", "Choose people other than the one getting the hours.")
        return data


class SplitForm(forms.Form):
    at = MinuteField(label="Split at", help_text="For example 2:00pm.")
