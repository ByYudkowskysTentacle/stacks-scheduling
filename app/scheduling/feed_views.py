# SPDX-License-Identifier: AGPL-3.0-or-later
"""The private calendar feed (SPEC §9.2): managing the link, and serving it to calendar apps."""

from __future__ import annotations

from django.contrib import messages
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View

from app.core.views_base import OrgViewMixin
from app.scheduling import feeds
from app.scheduling.models import CalendarFeed


class CalendarFeedView(View):
    """Served without signing in: calendar apps can't. The unguessable link is the secret,
    and an unknown, reset or archived link gets the same 404."""

    def get(self, request, token):
        employee = feeds.employee_for(token)
        if employee is None:
            raise Http404
        response = HttpResponse(feeds.build_ics(employee), content_type="text/calendar; charset=utf-8")
        response["Content-Disposition"] = 'inline; filename="shifts.ics"'
        response["Cache-Control"] = "private, max-age=900"
        response["X-Robots-Tag"] = "noindex"
        return response


class CalendarFeedManageView(OrgViewMixin, View):
    def employee(self, request):
        if request.access.employee is None:
            raise Http404("Your account isn't linked to an employee record.")
        return request.access.employee

    def get(self, request):
        employee = self.employee(request)
        has_feed = CalendarFeed.objects.filter(employee=employee).exists()
        return render(request, "scheduling/calendar_feed.html", {"has_feed": has_feed, "url": None})

    def post(self, request):
        employee = self.employee(request)
        if request.POST.get("action") == "remove":
            CalendarFeed.objects.filter(employee=employee).delete()
            messages.success(request, "Your calendar link no longer works.")
            return redirect("calendar_feed_manage")
        token = feeds.new_token(employee)
        url = request.build_absolute_uri(reverse("calendar_feed", args=[token]))
        return render(request, "scheduling/calendar_feed.html", {"has_feed": True, "url": url})
