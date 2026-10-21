# SPDX-License-Identifier: AGPL-3.0-or-later
"""Organization configuration (SPEC §3): organizations, locations, operating hours,
closures, terms and memberships. Nothing here is hard-coded to one library (CLAUDE.md §7)."""

from __future__ import annotations

from datetime import date

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from app.core.audit import audited
from app.core.fields import minute_field
from domain import calendar as cal
from domain.timeutil import Interval, is_on_slot

WEEKDAY_CHOICES = [(i, name) for i, name in enumerate(cal.WEEKDAY_NAMES)]


@audited()
class Organization(models.Model):
    name = models.CharField(max_length=200)
    time_zone = models.CharField(max_length=64, default="America/Chicago")
    week_start = models.PositiveSmallIntegerField(
        choices=WEEKDAY_CHOICES,
        default=cal.SAT,
        help_text="Weekly caps, planning periods and rotation weeks count from this day.",
    )
    planning_period_weeks = models.PositiveSmallIntegerField(
        default=2, validators=[MinValueValidator(1), MaxValueValidator(4)]
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.name


@audited()
class Location(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="locations")
    name = models.CharField(max_length=200)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["organization", "name"], name="location_unique_name")]

    def __str__(self) -> str:
        return self.name

    def domain_hours(self) -> list[cal.OperatingHours]:
        return [h.to_domain() for h in self.hours_sets.prefetch_related("intervals")]

    def domain_closures(self) -> list[cal.Closure]:
        return [c.to_domain() for c in self.closures.all()]


@audited()
class OperatingHoursSet(models.Model):
    """Weekly open intervals effective over a date range (SPEC §3.1). A later
    `effective_from` overrides an earlier set on the days both cover."""

    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="hours_sets")
    name = models.CharField(max_length=100, help_text='For example "Regular term hours" or "Finals week".')
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True, help_text="Inclusive. Leave blank for no end.")

    class Meta:
        ordering = ["effective_from"]
        verbose_name = "operating hours"
        verbose_name_plural = "operating hours"

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        if self.effective_to and self.effective_to < self.effective_from:
            raise ValidationError({"effective_to": "The end date is before the start date."})

    def to_domain(self) -> cal.OperatingHours:
        by_day: dict[int, list[Interval]] = {}
        for iv in self.intervals.all():
            by_day.setdefault(iv.weekday, []).append(Interval(iv.start_min, iv.end_min))
        return cal.OperatingHours(self.effective_from, self.effective_to, by_day)


@audited()
class OpenInterval(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    hours_set = models.ForeignKey(OperatingHoursSet, on_delete=models.CASCADE, related_name="intervals")
    weekday = models.PositiveSmallIntegerField(choices=WEEKDAY_CHOICES)
    start_min = minute_field("opens")
    end_min = minute_field("closes")

    class Meta:
        ordering = ["weekday", "start_min"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(start_min__lt=models.F("end_min")), name="open_interval_order"
            )
        ]

    def __str__(self) -> str:
        return f"{cal.WEEKDAY_NAMES[self.weekday]} {Interval(self.start_min, self.end_min).label()}"

    def clean(self) -> None:
        if not (is_on_slot(self.start_min) and is_on_slot(self.end_min)):
            raise ValidationError("Opening times must be on a 15-minute boundary.")


@audited()
class Closure(models.Model):
    """A time the location is closed inside operating hours: recurring (Chapel, Wednesdays
    10–11am) or dated (a holiday). No times means all day."""

    RECURRING, DATED = "recurring", "dated"

    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="closures")
    reason = models.CharField(max_length=200)
    kind = models.CharField(max_length=10, choices=[(RECURRING, "Recurring"), (DATED, "Dated")])
    weekday = models.PositiveSmallIntegerField(choices=WEEKDAY_CHOICES, null=True, blank=True)
    on_date = models.DateField("date", null=True, blank=True)
    valid_from = models.DateField(null=True, blank=True)
    valid_to = models.DateField(null=True, blank=True)
    start_min = minute_field("from", null=True, blank=True)
    end_min = minute_field("until", null=True, blank=True)

    class Meta:
        ordering = ["kind", "on_date", "weekday", "start_min"]

    def __str__(self) -> str:
        return self.reason

    @property
    def all_day(self) -> bool:
        return self.start_min is None

    def clean(self) -> None:
        errors: dict[str, str] = {}
        if self.kind == self.RECURRING and self.weekday is None:
            errors["weekday"] = "A recurring closure needs a weekday."
        if self.kind == self.DATED and self.on_date is None:
            errors["on_date"] = "A dated closure needs a date."
        if (self.start_min is None) != (self.end_min is None):
            errors["end_min"] = "Give both times, or neither for all day."
        elif self.start_min is not None and self.end_min is not None:
            if self.start_min >= self.end_min:
                errors["end_min"] = "The end is before the start."
            elif not (is_on_slot(self.start_min) and is_on_slot(self.end_min)):
                errors["start_min"] = "Closure times must be on a 15-minute boundary."
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            errors["valid_to"] = "The end date is before the start date."
        if errors:
            raise ValidationError(errors)

    def to_domain(self) -> cal.Closure:
        interval = (
            None if self.start_min is None or self.end_min is None else Interval(self.start_min, self.end_min)
        )
        if self.kind == self.DATED:
            return cal.Closure(reason=self.reason, interval=interval, on=self.on_date)
        return cal.Closure(
            reason=self.reason,
            interval=interval,
            weekday=self.weekday,
            valid_from=self.valid_from,
            valid_to=self.valid_to,
        )


def default_reminder_days() -> list[int]:
    return [3]


@audited()
class Term(models.Model):
    """An academic period. Availability, caps and funding are term-scoped (SPEC §3.2)."""

    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="terms")
    name = models.CharField(max_length=100)
    start_date = models.DateField()
    end_date = models.DateField()
    availability_deadline = models.DateField(
        null=True, blank=True, help_text="Optional. Shown to employees on their home and availability pages."
    )
    availability_reminders = models.BooleanField(
        "Email availability reminders",
        default=False,
        help_text="Off unless you turn it on. Emails people who haven't entered or confirmed "
        "their availability, a few days before the deadline and on the day.",
    )
    reminder_days = models.JSONField(
        default=default_reminder_days,
        blank=True,
        help_text="Days before the deadline to send a reminder, for example 7, 3, 1. One also goes out on the day.",
    )

    class Meta:
        ordering = ["-start_date"]
        constraints = [models.UniqueConstraint(fields=["organization", "name"], name="term_unique_name")]

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        if self.end_date and self.start_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "The term ends before it starts."})

    def contains(self, day: date) -> bool:
        return self.start_date <= day <= self.end_date

    @classmethod
    def current_for(cls, organization: Organization, today: date) -> Term | None:
        """The term containing `today`, else the next one to start, else the most recent."""
        terms = cls.objects.filter(organization=organization)
        return (
            terms.filter(start_date__lte=today, end_date__gte=today).first()
            or terms.filter(start_date__gt=today).order_by("start_date").first()
            or terms.order_by("-end_date").first()
        )


class Role(models.TextChoices):
    """Access roles (SPEC §1). Shift-lead rights are granted per employee, not by role (D25)."""

    ADMIN = "admin", "Admin"
    SUPERVISOR = "supervisor", "Supervisor"
    EMPLOYEE = "employee", "Employee"


@audited()
class Department(models.Model):
    """A team with its own schedules, for example Circulation or Special Collections (D95).
    Employees can belong to several. An admin assigns each supervisor one or more."""

    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="departments")
    name = models.CharField(max_length=100)
    # Coverage for this department's schedules; blank uses the organization's scheduling rules.
    coverage_default = models.PositiveSmallIntegerField(
        "people on duty per slot",
        null=True,
        blank=True,
        help_text="Blank uses the organization's setting under Scheduling rules.",
    )
    coverage_mode = models.CharField(
        max_length=10,
        blank=True,
        choices=[("exact", "Exact: report under and over"), ("minimum", "Minimum: report under only")],
        help_text="Blank uses the organization's setting.",
    )
    # Other departments whose open shifts this department's people may see and ask for (D108).
    open_shifts_from = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["organization", "name"], name="department_name_once")]

    def __str__(self) -> str:
        return self.name


@audited()
class Membership(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.EMPLOYEE)
    created_at = models.DateTimeField(auto_now_add=True)
    departments = models.ManyToManyField(
        Department, through="SupervisorDepartment", related_name="supervisors", blank=True
    )

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "organization"], name="membership_unique")]

    def __str__(self) -> str:
        return f"{self.user} ({self.get_role_display()})"


@audited()
class SupervisorDepartment(models.Model):
    """A department a supervisor manages (D95). Admins manage every department."""

    membership = models.ForeignKey(Membership, on_delete=models.CASCADE, related_name="department_links")
    department = models.ForeignKey(Department, on_delete=models.CASCADE, related_name="supervisor_links")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["membership", "department"], name="supervisor_department_once")
        ]

    def __str__(self) -> str:
        return f"{self.membership}: {self.department}"


def _days(label: str, help_text: str) -> models.PositiveIntegerField:
    return models.PositiveIntegerField(
        label, null=True, blank=True, validators=[MinValueValidator(1)], help_text=help_text
    )


@audited()
class RetentionPolicy(models.Model):
    """How long each kind of personal data is kept (SPEC §11). Blank means keep: nothing is
    purged until an administrator sets a period. Purges run daily in the worker, or on demand.
    They keep the audit history of what they remove; the audit log has its own period (D109)."""

    organization = models.OneToOneField(Organization, on_delete=models.CASCADE, related_name="retention")
    availability_days = _days("Term availability", "Days after the term ends, for example 365.")
    time_off_days = _days("Time-off requests", "Days after the time off ends.")
    import_days = _days("Import data", "Days after an import: the parsed rows, preview and undo data.")
    notification_days = _days("In-app notifications", "Days after a notification was sent.")
    solve_run_days = _days(
        "Solver runs", "Days after a generate or rebalance finished: options and explanations."
    )
    audit_days = _days(
        "Audit log",
        "Days after a change was recorded. Covers the history of everything above too, so set it "
        "no shorter than you need records of who changed what.",
    )
    last_purged_at = models.DateTimeField(null=True, blank=True)
    last_purge_counts = models.JSONField(default=dict, blank=True)

    def __str__(self) -> str:
        return f"Retention policy for {self.organization}"
