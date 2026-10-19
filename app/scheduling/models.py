# SPDX-License-Identifier: AGPL-3.0-or-later
"""Schedules (SPEC §6, §9).

The `Shift` table is the working copy, and every edit to it is a draft. Publishing
snapshots the working copy into a `PublishedVersion`; students only ever see published
versions, and notifications go out only on publish (CLAUDE.md §2.2). Shifts are never
hard-deleted: removal sets `deleted_at`, so history and IDs survive (CLAUDE.md §2.3).
"""

from __future__ import annotations

from datetime import date, timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from app.core.audit import audited
from app.core.fields import minute_field
from app.core.models import Location, Organization
from app.people.models import Employee, PayRole, Tag
from domain import rules as dom
from domain.timeutil import Interval, is_on_slot


@audited()
class SchedulingRules(models.Model):
    """Organization-wide scheduling settings (SPEC §6.1, §6.2, §6.4). Stored in minutes.

    Coverage defaults live here rather than per location because v1 has one location (D1);
    coverage overrides are already per location.
    """

    organization = models.OneToOneField(
        Organization, on_delete=models.CASCADE, related_name="scheduling_rules"
    )
    min_shift_minutes = models.PositiveSmallIntegerField("minimum shift (minutes)", default=120)
    max_shift_minutes = models.PositiveSmallIntegerField("maximum shift (minutes)", default=240)
    max_daily_minutes = models.PositiveSmallIntegerField("maximum per person per day (minutes)", default=360)
    max_shifts_per_day = models.PositiveSmallIntegerField(default=2)
    min_gap_minutes = models.PositiveSmallIntegerField(
        "minimum gap between a person's shifts (minutes)", default=30
    )
    default_weekly_cap_minutes = models.PositiveSmallIntegerField(
        "default weekly cap (minutes)",
        null=True,
        blank=True,
        default=1200,
        help_text="Blank means no default cap.",
    )
    coverage_default = models.PositiveSmallIntegerField("people on duty per slot", default=2)
    coverage_mode = models.CharField(
        max_length=10,
        choices=[
            (dom.CoverageMode.EXACT, "Exact: report under and over"),
            (dom.CoverageMode.MINIMUM, "Minimum: report under only"),
        ],
        default=dom.CoverageMode.EXACT,
    )
    freeze_window_hours = models.PositiveSmallIntegerField(
        default=48, help_text="The solver never moves shifts starting within this many hours. 0 turns it off."
    )
    change_limit_percent = models.PositiveSmallIntegerField(
        default=30,
        validators=[MaxValueValidator(100)],
        help_text="Limit for the limited-change rebalance option.",
    )

    class Meta:
        verbose_name_plural = "scheduling rules"

    def __str__(self) -> str:
        return f"Scheduling rules for {self.organization}"

    @classmethod
    def for_org(cls, organization: Organization) -> SchedulingRules:
        rules, _ = cls.objects.get_or_create(organization=organization)
        return rules

    def shape(self) -> dom.ShapeRules:
        return dom.ShapeRules(
            self.min_shift_minutes,
            self.max_shift_minutes,
            self.max_daily_minutes,
            self.max_shifts_per_day,
            self.min_gap_minutes,
        )


@audited()
class CoverageOverride(models.Model):
    """People on duty for some weekdays and times, for example 3 from 10am to 4pm on weekdays."""

    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="coverage_overrides")
    weekdays = models.JSONField(default=list, help_text="Weekday numbers, Monday 0.")
    start_min = minute_field("from")
    end_min = minute_field("until")
    count = models.PositiveSmallIntegerField("people on duty")

    class Meta:
        ordering = ["start_min"]

    def __str__(self) -> str:
        return f"{self.count} on duty {Interval(self.start_min, self.end_min).label()}"

    def to_domain(self) -> dom.CoverageOverride:
        return dom.CoverageOverride(
            frozenset(self.weekdays), Interval(self.start_min, self.end_min), self.count
        )


@audited()
class HourCapRule(models.Model):
    """A cap on scheduled hours for one employee, or everyone with a tag, or everyone (SPEC §6.4)."""

    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="hour_caps")
    scope = models.CharField(max_length=10, choices=[(s.value, s.value.capitalize()) for s in dom.CapScope])
    minutes = models.PositiveIntegerField(validators=[MinValueValidator(0)])
    employee = models.ForeignKey(
        Employee, on_delete=models.CASCADE, null=True, blank=True, related_name="hour_caps"
    )
    tag = models.ForeignKey(Tag, on_delete=models.PROTECT, null=True, blank=True, related_name="hour_caps")
    label = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ["scope", "minutes"]
        constraints = [
            models.CheckConstraint(
                condition=~(models.Q(employee__isnull=False) & models.Q(tag__isnull=False)),
                name="cap_employee_or_tag",
            )
        ]

    def __str__(self) -> str:
        who = self.employee or (f"{self.tag} tag" if self.tag else "everyone")
        return f"{who}: {self.minutes / 60:g}h per {self.scope}"

    def to_domain(self) -> dom.HourCap:
        return dom.HourCap(
            dom.CapScope(self.scope),
            self.minutes,
            employee_id=self.employee_id,
            tag=self.tag.name if self.tag else None,
            label=self.label,
        )


@audited()
class TagCoverageRule(models.Model):
    """Skill coverage (SPEC §6.3)."""

    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="tag_rules")
    tag = models.ForeignKey(Tag, on_delete=models.PROTECT, related_name="coverage_rules")
    kind = models.CharField(
        max_length=20,
        choices=[
            (dom.TagRuleKind.MIN_ON_DUTY, "At least N on duty"),
            (dom.TagRuleKind.MIN_WEEKLY_HOURS, "Total hours per week"),
            (dom.TagRuleKind.NOT_ALONE, "Never the only person on duty"),
        ],
    )
    hard = models.BooleanField(default=False, help_text="Hard rules are never broken by the generator.")
    count = models.PositiveSmallIntegerField(default=1)
    minutes = models.PositiveIntegerField(default=0, help_text="For weekly-hours rules.")
    weekdays = models.JSONField(
        default=list, blank=True, help_text="Weekday numbers, Monday 0. Empty means every day."
    )
    start_min = minute_field("from", null=True, blank=True)
    end_min = minute_field("until", null=True, blank=True)
    label = models.CharField(max_length=150, blank=True)

    class Meta:
        ordering = ["tag__name", "kind"]

    def __str__(self) -> str:
        return self.to_domain().describe()

    def to_domain(self) -> dom.TagRule:
        interval = (
            Interval(self.start_min, self.end_min)
            if self.start_min is not None and self.end_min is not None
            else None
        )
        return dom.TagRule(
            tag=self.tag.name,
            kind=dom.TagRuleKind(self.kind),
            hard=self.hard,
            count=self.count,
            minutes=self.minutes,
            weekdays=frozenset(self.weekdays) if self.weekdays else frozenset(range(7)),
            interval=interval,
            label=self.label,
        )


@audited()
class SchedulePeriod(models.Model):
    """A planning period (SPEC §3.2): one to four weeks from a week start."""

    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="periods")
    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="periods")
    start_date = models.DateField()
    weeks = models.PositiveSmallIntegerField(
        default=2, validators=[MinValueValidator(1), MaxValueValidator(4)]
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-start_date"]
        constraints = [models.UniqueConstraint(fields=["location", "start_date"], name="period_unique_start")]

    def __str__(self) -> str:
        return f"{self.start_date:%-d %b} to {self.end_date:%-d %b %Y}"

    @property
    def end_date(self) -> date:
        return self.start_date + timedelta(days=self.weeks * 7 - 1)

    @property
    def days(self) -> list[date]:
        return [self.start_date + timedelta(days=i) for i in range(self.weeks * 7)]

    def contains(self, day: date) -> bool:
        return self.start_date <= day <= self.end_date

    def latest_version(self) -> PublishedVersion | None:
        return self.versions.order_by("-number").first()


class ShiftQuerySet(models.QuerySet["Shift"]):
    def live(self) -> ShiftQuerySet:
        return self.filter(deleted_at__isnull=True)


@audited()
class Shift(models.Model):
    """One assignment in the working copy. `employee` empty means an open shift."""

    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    location = models.ForeignKey(Location, on_delete=models.PROTECT)
    period = models.ForeignKey(SchedulePeriod, on_delete=models.PROTECT, related_name="shifts")
    employee = models.ForeignKey(
        Employee, on_delete=models.PROTECT, null=True, blank=True, related_name="shifts"
    )
    date = models.DateField()
    start_min = minute_field("start")
    end_min = minute_field("end")
    pay_role = models.ForeignKey(PayRole, on_delete=models.PROTECT, null=True, blank=True)
    note = models.CharField(max_length=200, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    proposed_by_lead = models.BooleanField(
        default=False, help_text="Last changed by a shift lead; shown in the supervisor's review queue (D25)."
    )

    objects = ShiftQuerySet.as_manager()

    class Meta:
        ordering = ["date", "start_min", "employee__preferred_name"]
        constraints = [
            models.CheckConstraint(condition=models.Q(start_min__lt=models.F("end_min")), name="shift_order")
        ]
        indexes = [models.Index(fields=["period", "date"])]

    def __str__(self) -> str:
        who = self.employee.display_name if self.employee else "Open shift"
        return f"{who} {self.date:%a %-d %b} {self.interval.label()}"

    @property
    def interval(self) -> Interval:
        return Interval(self.start_min, self.end_min)

    @property
    def minutes(self) -> int:
        return self.end_min - self.start_min

    def clean(self) -> None:
        if self.start_min is None or self.end_min is None:
            return
        if self.start_min >= self.end_min:
            raise ValidationError({"end_min": "The shift ends before it starts."})
        if not (is_on_slot(self.start_min) and is_on_slot(self.end_min)):
            raise ValidationError({"start_min": "Shifts start and end on 15-minute boundaries."})
        if self.period_id and self.date and not self.period.contains(self.date):
            raise ValidationError({"date": f"Pick a date in this period ({self.period})."})

    def snapshot(self) -> dict:
        return {
            "id": self.pk,
            "employee_id": self.employee_id,
            "date": self.date.isoformat(),
            "start": self.start_min,
            "end": self.end_min,
            "pay_role_id": self.pay_role_id,
            "location_id": self.location_id,
            "note": self.note,
        }


@audited()
class PublishedVersion(models.Model):
    """An immutable snapshot of a period's schedule as published (SPEC §9.1)."""

    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    period = models.ForeignKey(SchedulePeriod, on_delete=models.PROTECT, related_name="versions")
    number = models.PositiveIntegerField()
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    published_at = models.DateTimeField(auto_now_add=True)
    shifts = models.JSONField(default=list)
    validation_summary = models.CharField(max_length=300, blank=True)
    issues = models.JSONField(default=list)
    changes = models.JSONField(
        default=dict, help_text="Per-employee summary of what changed from the version before."
    )

    class Meta:
        ordering = ["-number"]
        constraints = [models.UniqueConstraint(fields=["period", "number"], name="version_unique_number")]

    def __str__(self) -> str:
        return f"Version {self.number} of {self.period}"


@audited()
class CallOut(models.Model):
    """A day-of call-out and who covered it, recorded by a shift lead or supervisor (D25).
    Always flagged for supervisor review; never notifies anyone by itself."""

    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    shift = models.ForeignKey(Shift, on_delete=models.PROTECT, related_name="call_outs")
    called_out = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="call_outs")
    covered_by = models.ForeignKey(
        Employee, on_delete=models.PROTECT, null=True, blank=True, related_name="covers"
    )
    note = models.CharField(max_length=200, blank=True, help_text="No medical or personal details.")
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    recorded_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-recorded_at"]

    def __str__(self) -> str:
        return f"Call-out: {self.called_out} for {self.shift}"


class Notification(models.Model):
    """In-app notification (D14). Created only by publishing."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    created_at = models.DateTimeField(auto_now_add=True)
    subject = models.CharField(max_length=200)
    body = models.TextField()
    link = models.CharField(max_length=200, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.subject
