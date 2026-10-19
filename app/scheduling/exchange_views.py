# SPDX-License-Identifier: AGPL-3.0-or-later
"""Open shifts and cover offers for employees (SPEC §9.2)."""

from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.generic import TemplateView

from app.core.views_base import OrgViewMixin
from app.scheduling import exchanges
from app.scheduling.models import ACTIVE_EXCHANGE, ExchangeKind, ExchangeStatus, Shift, ShiftExchange
from app.scheduling.published import my_upcoming, published_week
from domain import calendar as cal

WEEKS = 4


class ShiftExchangeView(OrgViewMixin, TemplateView):
    template_name = "scheduling/exchange.html"

    def employee(self):
        if self.request.access.employee is None:
            raise Http404("Your account isn't linked to an employee record.")
        return self.request.access.employee

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        me = self.employee()
        org = me.organization
        today = timezone.localdate()
        start = cal.week_start_for(today, org.week_start)
        active = ShiftExchange.objects.filter(organization=org, status__in=ACTIVE_EXCHANGE)
        my_claims = set(active.filter(taker=me, kind=ExchangeKind.CLAIM).values_list("shift_id", flat=True))
        offered = set(active.values_list("shift_id", flat=True))
        open_shifts = [
            s
            for w in range(WEEKS)
            for d in published_week(org, start + timedelta(days=7 * w), me)
            for s in d.shifts
            if s.employee_id is None and s.day >= today and s.shift_id
        ]
        ctx.update(
            open_shifts=[(s, s.shift_id in my_claims) for s in open_shifts],
            offers=active.filter(kind=ExchangeKind.COVER, status=ExchangeStatus.OPEN, shift__date__gte=today)
            .exclude(requester=me)
            .select_related("shift__location", "requester"),
            my_shifts=[(m, m.shift.shift_id in offered) for m in my_upcoming(me, today, WEEKS)],
            my_requests=ShiftExchange.objects.filter(Q(requester=me) | Q(taker=me))
            .select_related("shift", "requester", "taker")
            .order_by("-updated_at")[:20],
            me=me,
        )
        return ctx

    def post(self, request, *args, **kwargs):
        me = self.employee()
        today = timezone.localdate()
        action = request.POST.get("action")
        try:
            if action in ("claim", "offer"):
                shift = get_object_or_404(
                    Shift.objects.filter(organization=me.organization).select_related("period"),
                    pk=request.POST.get("shift"),
                )
                if action == "claim":
                    exchanges.claim(me, shift, today)
                    messages.success(
                        request,
                        f"Asked for {shift.date:%a %-d %b} {shift.interval.label()}. A supervisor will decide.",
                    )
                else:
                    exchanges.offer(me, shift, today)
                    messages.success(
                        request,
                        "Offered your shift. You're still on it until a supervisor approves a volunteer.",
                    )
            elif action in ("volunteer", "withdraw"):
                ex = get_object_or_404(
                    ShiftExchange.objects.filter(organization=me.organization),
                    pk=request.POST.get("exchange"),
                )
                if action == "volunteer":
                    exchanges.volunteer(me, ex, today)
                    messages.success(request, "Thanks. A supervisor will confirm before it's yours.")
                else:
                    exchanges.withdraw(me, ex)
                    messages.success(request, "Withdrawn.")
        except exchanges.ExchangeError as exc:
            messages.error(request, str(exc))
        return redirect("shift_exchange")
