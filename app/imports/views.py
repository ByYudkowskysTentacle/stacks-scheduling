# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.views.generic import TemplateView

from app.core.models import Term
from app.core.permissions import Perm
from app.core.views_base import OrgViewMixin
from app.imports import services
from app.imports.models import BatchKind, BatchStatus, ImportBatch


class UploadForm(forms.Form):
    kind = forms.ChoiceField(choices=BatchKind.choices, widget=forms.RadioSelect, label="What's in the file")
    term = forms.ModelChoiceField(
        queryset=Term.objects.none(), required=False, help_text="For availability only."
    )
    file = forms.FileField(
        label="CSV file", widget=forms.ClearableFileInput(attrs={"accept": ".csv,text/csv"})
    )

    def __init__(self, *args, organization, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["term"].queryset = Term.objects.filter(organization=organization)

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("kind") == BatchKind.AVAILABILITY and not cleaned.get("term"):
            self.add_error("term", "Choose the term this availability is for.")
        return cleaned


class ImportListView(OrgViewMixin, TemplateView):
    permission = Perm.MANAGE_PEOPLE
    template_name = "imports/list.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.setdefault(
            "form", UploadForm(organization=self.request.organization, initial={"kind": BatchKind.ROSTER})
        )
        ctx["batches"] = self.org_queryset(ImportBatch).select_related("term", "created_by")[:20]
        return ctx

    def post(self, request, *args, **kwargs):
        form = UploadForm(request.POST, request.FILES, organization=request.organization)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))
        upload = form.cleaned_data["file"]
        try:
            text = services.decode_upload(upload.read())
            batch = services.create_batch(
                request.organization,
                request.user,
                form.cleaned_data["kind"],
                text,
                upload.name,
                form.cleaned_data["term"],
            )
        except services.ImportProblem as exc:
            form.add_error("file", str(exc))
            return self.render_to_response(self.get_context_data(form=form))
        return redirect("import_detail", pk=batch.pk)


class ImportFormatsView(OrgViewMixin, TemplateView):
    permission = Perm.MANAGE_PEOPLE
    template_name = "imports/formats.html"


class ImportDetailView(OrgViewMixin, TemplateView):
    permission = Perm.MANAGE_PEOPLE
    template_name = "imports/detail.html"

    def get_batch(self) -> ImportBatch:
        return get_object_or_404(self.org_queryset(ImportBatch), pk=self.kwargs["pk"])

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        batch = self.get_batch()
        ctx.update(batch=batch, plan=batch.plan, can_undo=services.can_undo(batch), Status=BatchStatus)
        return ctx

    def post(self, request, *args, **kwargs):
        batch = self.get_batch()
        action = request.POST.get("action")
        try:
            if action == "apply":
                services.apply(batch)
                messages.success(request, "Import applied. You can undo it from this page.")
            elif action == "discard" and batch.status == BatchStatus.PREVIEW:
                batch.status = BatchStatus.DISCARDED
                batch.payload = []
                batch.save(update_fields=["status", "payload"])
                messages.success(request, "Import discarded. Nothing was changed.")
            elif action == "undo":
                if request.POST.get("confirm") != "yes":
                    messages.error(request, "Tick the confirmation box to undo.")
                else:
                    services.undo(batch)
                    messages.success(request, "Import undone. The previous records are restored.")
        except services.ImportProblem as exc:
            messages.error(request, str(exc))
        return redirect("import_detail", pk=batch.pk)
