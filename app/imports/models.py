# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bulk imports (SPEC §9.3): previewed, applied in one transaction, and undoable.

The parsed rows and the before-state needed for undo are kept on the batch. They
are personal data, so they live only in the database, never in logs or files.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from app.core.audit import audited
from app.core.models import Organization, Term


class BatchKind(models.TextChoices):
    ROSTER = "roster", "Roster"
    AVAILABILITY = "availability", "Availability"


class BatchStatus(models.TextChoices):
    PREVIEW = "preview", "Preview"
    APPLIED = "applied", "Applied"
    UNDONE = "undone", "Undone"
    DISCARDED = "discarded", "Discarded"


@audited()
class ImportBatch(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name="import_batches")
    kind = models.CharField(max_length=20, choices=BatchKind.choices)
    term = models.ForeignKey(Term, on_delete=models.PROTECT, null=True, blank=True)
    status = models.CharField(max_length=20, choices=BatchStatus.choices, default=BatchStatus.PREVIEW)
    filename = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    undone_at = models.DateTimeField(null=True, blank=True)
    payload = models.JSONField(default=list, help_text="Parsed rows waiting to be applied.")
    plan = models.JSONField(default=dict, help_text="What applying will do, shown in the preview.")
    undo = models.JSONField(default=dict, help_text="Before-state for undo.")

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "import batches"

    def __str__(self) -> str:
        return f"{self.get_kind_display()} import #{self.pk}"
