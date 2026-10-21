# SPDX-License-Identifier: AGPL-3.0-or-later
"""People (SPEC §4): employees, pay roles with wages, and tags.

Wages and supervisor notes are visible to supervisors and admins only (D48, D25);
the permission layer enforces that, and the views never render them otherwise.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models

from app.core.audit import audited
from app.core.models import Department, Organization

COLOR_VALIDATOR = RegexValidator(r"^#[0-9a-fA-F]{6}$", "Use a hex colour such as #3366cc.")
WAGE_VALIDATORS = [MinValueValidator(Decimal("0.00"))]


@audited()
class PayRole(models.Model):
    """The paid position a shift is worked under. Maps to Homebase "roles" (SPEC §2)."""

    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="pay_roles")
    name = models.CharField(max_length=100)
    color = models.CharField(max_length=7, default="#3366cc", validators=[COLOR_VALIDATOR])
    paycom_code = models.CharField(
        "Paycom pay code", max_length=50, blank=True, help_text="Optional. Used only by a payroll export."
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["organization", "name"], name="payrole_unique_name")]

    def __str__(self) -> str:
        return self.name


@audited()
class Tag(models.Model):
    """A skill or duty label used by coverage rules (SPEC §4.3). Tags never grant access."""

    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="tags")
    name = models.CharField(max_length=60)
    description = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["organization", "name"], name="tag_unique_name")]

    def __str__(self) -> str:
        return self.name


class EmploymentType(models.TextChoices):
    REGULAR = "regular", "Regular"
    NON_REGULAR = "non_regular", "Non-regular (never auto-scheduled)"
    INACTIVE = "inactive", "Inactive"


class EmployeeQuerySet(models.QuerySet["Employee"]):
    def current(self) -> EmployeeQuerySet:
        return self.filter(archived_at__isnull=True)

    def schedulable(self) -> EmployeeQuerySet:
        return self.current().filter(employment_type=EmploymentType.REGULAR, in_scheduling_pool=True)


@audited()
class Employee(models.Model):
    """A person who can be scheduled (SPEC §4.1)."""

    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="employees")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="employee_records",
        help_text="The sign-in account, once the person has been invited.",
    )
    full_name = models.CharField(max_length=200)
    preferred_name = models.CharField(
        max_length=100, help_text="Shown to co-workers on the published schedule."
    )
    campus_id = models.CharField("campus ID", max_length=50, blank=True)
    email = models.EmailField(blank=True)
    employment_type = models.CharField(
        max_length=20, choices=EmploymentType.choices, default=EmploymentType.REGULAR
    )
    hire_date = models.DateField(null=True, blank=True)
    new_employee_until = models.DateField(
        null=True, blank=True, help_text="While set and in the future, new-employee rules apply."
    )
    default_pay_role = models.ForeignKey(
        PayRole, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    default_wage = models.DecimalField(
        "default hourly wage",
        max_digits=7,
        decimal_places=2,
        null=True,
        blank=True,
        validators=WAGE_VALIDATORS,
    )
    notes = models.TextField(
        "supervisor notes", blank=True, help_text="Visible to supervisors and admins only."
    )
    in_scheduling_pool = models.BooleanField(
        default=True, help_text="Off removes the person from scheduling without archiving them."
    )
    shift_lead_access = models.BooleanField(
        "shift-lead permission",
        default=False,
        help_text="Lets this person propose draft edits and see co-workers' free/busy and hours. "
        "Separate from the Shift lead tag.",
    )
    archived_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    departments = models.ManyToManyField(
        Department,
        through="EmployeeDepartment",
        related_name="employees",
        blank=True,
        help_text="The departments whose schedules this person is on.",
    )

    objects = EmployeeQuerySet.as_manager()

    class Meta:
        ordering = ["preferred_name", "full_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "user"],
                condition=models.Q(user__isnull=False),
                name="employee_unique_user",
            ),
            models.UniqueConstraint(
                fields=["organization", "campus_id"],
                condition=~models.Q(campus_id=""),
                name="employee_unique_campus_id",
            ),
        ]

    def __str__(self) -> str:
        return self.preferred_name or self.full_name

    def save(self, *args, **kwargs) -> None:
        new = self._state.adding
        super().save(*args, **kwargs)
        if new:
            # In a library with one department, everyone is on its roster (D95).
            only = list(Department.objects.filter(organization_id=self.organization_id)[:2])
            if len(only) == 1:
                EmployeeDepartment.objects.get_or_create(
                    organization_id=self.organization_id, employee=self, department=only[0]
                )

    @property
    def display_name(self) -> str:
        return self.preferred_name or self.full_name

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    def is_new_employee(self, on: date) -> bool:
        return self.new_employee_until is not None and on <= self.new_employee_until

    def wage_for(self, pay_role: PayRole | None) -> Decimal | None:
        """The rate for a pay role, falling back to the default wage (SPEC §4.2)."""
        if pay_role is not None:
            for epr in self.pay_roles.all():
                if epr.pay_role_id == pay_role.pk and epr.wage is not None:
                    return epr.wage
        return self.default_wage

    def tags_on(self, day: date) -> list[Tag]:
        return [et.tag for et in self.employee_tags.all() if et.active_on(day)]


@audited()
class EmployeePayRole(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="pay_roles")
    pay_role = models.ForeignKey(PayRole, on_delete=models.PROTECT, related_name="employee_rates")
    wage = models.DecimalField(
        "hourly wage",
        max_digits=7,
        decimal_places=2,
        null=True,
        blank=True,
        validators=WAGE_VALIDATORS,
        help_text="Leave blank to use the employee's default wage.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["employee", "pay_role"], name="employee_payrole_unique")
        ]

    def __str__(self) -> str:
        return f"{self.employee} as {self.pay_role}"


@audited()
class EmployeeDepartment(models.Model):
    """An employee's place on a department's roster (D95)."""

    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="department_links")
    department = models.ForeignKey(Department, on_delete=models.PROTECT, related_name="employee_links")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["employee", "department"], name="employee_department_once")
        ]

    def __str__(self) -> str:
        return f"{self.employee}: {self.department}"


@audited()
class EmployeeTag(models.Model):
    """A tag held by an employee, optionally time-bound (`Shift lead` for one week only)."""

    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="employee_tags")
    tag = models.ForeignKey(Tag, on_delete=models.PROTECT, related_name="employee_tags")
    valid_from = models.DateField(null=True, blank=True)
    valid_to = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["tag__name"]

    def __str__(self) -> str:
        return f"{self.employee}: {self.tag}"

    def clean(self) -> None:
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            raise ValidationError({"valid_to": "The end date is before the start date."})

    def active_on(self, day: date) -> bool:
        return (self.valid_from is None or self.valid_from <= day) and (
            self.valid_to is None or day <= self.valid_to
        )
