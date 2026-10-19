# SPDX-License-Identifier: AGPL-3.0-or-later
"""Open shifts, cover offers and swaps for employees (SPEC §9.2, D104)."""

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
        offered = set(active.values_list("shift_id", flat=True)) | set(
            active.exclude(other_shift=None).values_list("other_shift_id", flat=True)
        )
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
            swaps_for_me=active.filter(
                kind=ExchangeKind.SWAP, status=ExchangeStatus.ASKED, taker=me
            ).select_related("shift", "other_shift", "requester"),
            my_requests=ShiftExchange.objects.filter(Q(requester=me) | Q(taker=me))
            .select_related("shift", "other_shift", "requester", "taker")
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
            elif action in ("volunteer", "withdraw", "accept_swap", "decline_swap"):
                ex = get_object_or_404(
                    ShiftExchange.objects.filter(organization=me.organization),
                    pk=request.POST.get("exchange"),
                )
                if action == "volunteer":
                    exchanges.volunteer(me, ex, today)
                    messages.success(request, "Thanks. A supervisor will confirm before it's yours.")
                elif action == "accept_swap":
                    exchanges.respond(me, ex, True, today)
                    messages.success(request, "Accepted. A supervisor will confirm the swap.")
                elif action == "decline_swap":
                    exchanges.respond(me, ex, False, today)
                    messages.success(request, "Declined. Your shift stays as it is.")
                else:
                    exchanges.withdraw(me, ex)
                    messages.success(request, "Withdrawn.")
        except exchanges.ExchangeError as exc:
            messages.error(request, str(exc))
        return redirect("shift_exchange")


class SwapView(ShiftExchangeView):
    """Pick a co-worker's published shift to swap one of yours for (D104)."""

    template_name = "scheduling/swap.html"

    def mine(self, me) -> Shift:
        shift = get_object_or_404(
            Shift.objects.live().filter(organization=me.organization, employee=me).select_related("period"),
            pk=self.kwargs["shift_pk"],
        )
        return shift

    def get_context_data(self, **kwargs):
        ctx = TemplateView.get_context_data(self, **kwargs)
        me = self.employee()
        org = me.organization
        shift = self.mine(me)
        today = timezone.localdate()
        start = cal.week_start_for(today, org.week_start)
        busy = set(
            ShiftExchange.objects.filter(organization=org, status__in=ACTIVE_EXCHANGE).values_list(
                "shift_id", flat=True
            )
        ) | set(
            ShiftExchange.objects.filter(organization=org, status__in=ACTIVE_EXCHANGE)
            .exclude(other_shift=None)
            .values_list("other_shift_id", flat=True)
        )
        mine = [
            (s.day, s.interval)
            for w in range(WEEKS)
            for d in published_week(org, start + timedelta(days=7 * w), me)
            for s in d.shifts
            if s.employee_id == me.pk and s.shift_id != shift.pk
        ]
        ctx.update(
            shift=shift,
            choices=[
                s
                for w in range(WEEKS)
                for d in published_week(org, start + timedelta(days=7 * w), me)
                for s in d.shifts
                if s.employee_id not in (None, me.pk)
                and s.day >= today
                and s.shift_id
                and s.shift_id not in busy
                and not any(day == s.day and iv.overlaps(s.interval) for day, iv in mine)
            ],
        )
        return ctx

    def post(self, request, *args, **kwargs):
        me = self.employee()
        shift = self.mine(me)
        other = get_object_or_404(
            Shift.objects.filter(organization=me.organization).select_related("period", "employee"),
            pk=request.POST.get("other"),
        )
        try:
            exchanges.swap(me, shift, other, timezone.localdate())
        except exchanges.ExchangeError as exc:
            messages.error(request, str(exc))
            return redirect("shift_swap", shift_pk=shift.pk)
        messages.success(
            request,
            "Asked. If your co-worker accepts, a supervisor decides. Your shift stays yours until then.",
        )
        return redirect("shift_exchange")
