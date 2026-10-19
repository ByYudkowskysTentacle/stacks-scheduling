# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from django import forms

from app.core.forms import DateInput
from app.core.models import Department
from app.people.models import Employee, EmployeeDepartment, EmployeePayRole, EmployeeTag, PayRole, Tag


class PayRoleForm(forms.ModelForm):
    class Meta:
        model = PayRole
        fields = ["name", "color", "paycom_code", "is_active"]
        widgets = {"color": forms.TextInput(attrs={"type": "color"})}


class TagForm(forms.ModelForm):
    class Meta:
        model = Tag
        fields = ["name", "description"]


class EmployeeForm(forms.ModelForm):
    """Supervisor edit form. Wage and notes fields are removed for anyone without the
    matching permission, so they are neither shown nor accepted (D48)."""

    class Meta:
        model = Employee
        fields = [
            "full_name",
            "preferred_name",
            "email",
            "campus_id",
            "employment_type",
            "hire_date",
            "new_employee_until",
            "default_pay_role",
            "default_wage",
            "in_scheduling_pool",
            "shift_lead_access",
            "notes",
        ]
        widgets = {
            "hire_date": DateInput(),
            "new_employee_until": DateInput(),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    department_choices = forms.ModelMultipleChoiceField(
        queryset=Department.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Departments",
        help_text="The departments whose schedules this person is on. Someone in none appears on no schedule.",
    )

    def __init__(self, *args, organization, can_view_pay: bool, can_view_notes: bool, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.organization = organization
        self.fields["default_pay_role"].queryset = PayRole.objects.filter(
            organization=organization, is_active=True
        )
        departments = organization.departments.all()
        self.fields["department_choices"].queryset = departments
        if self.instance.pk:
            self.fields["department_choices"].initial = list(self.instance.departments.all())
        elif departments.count() == 1:
            self.fields["department_choices"].initial = list(departments)
        if not can_view_pay:
            del self.fields["default_wage"]
        if not can_view_notes:
            del self.fields["notes"]

    def clean_email(self):
        return (self.cleaned_data.get("email") or "").strip().lower()

    def save_departments(self, employee: Employee) -> None:
        """Make the employee's department links match the ticked boxes (D95)."""
        wanted = set(self.cleaned_data.get("department_choices") or [])
        current = {link.department: link for link in employee.department_links.select_related("department")}
        for dept, link in current.items():
            if dept not in wanted:
                link.delete()
        for dept in wanted - set(current):
            EmployeeDepartment.objects.create(
                organization=employee.organization, employee=employee, department=dept
            )

    def clean_preferred_name(self):
        return (self.cleaned_data.get("preferred_name") or "").strip()


class EmployeePayRoleForm(forms.ModelForm):
    class Meta:
        model = EmployeePayRole
        fields = ["pay_role", "wage"]


class EmployeeTagForm(forms.ModelForm):
    class Meta:
        model = EmployeeTag
        fields = ["tag", "valid_from", "valid_to"]
        widgets = {"valid_from": DateInput(), "valid_to": DateInput()}
        help_texts = {"valid_from": "Optional. Leave both dates blank for an ongoing tag."}


def employee_pay_role_formset(organization, **kwargs):
    FormSet = forms.inlineformset_factory(
        Employee, EmployeePayRole, form=EmployeePayRoleForm, extra=1, can_delete=True
    )
    formset = FormSet(prefix="payroles", **kwargs)
    for form in formset.forms:
        form.fields["pay_role"].queryset = PayRole.objects.filter(organization=organization)
        form.instance.organization = organization
    return formset


def employee_tag_formset(organization, **kwargs):
    FormSet = forms.inlineformset_factory(
        Employee, EmployeeTag, form=EmployeeTagForm, extra=2, can_delete=True
    )
    formset = FormSet(prefix="tags", **kwargs)
    for form in formset.forms:
        form.fields["tag"].queryset = Tag.objects.filter(organization=organization)
        form.instance.organization = organization
    return formset
