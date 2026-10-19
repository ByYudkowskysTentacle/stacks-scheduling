# SPDX-License-Identifier: AGPL-3.0-or-later
from django.contrib import admin
from django.urls import include, path

from app.availability import views as availability
from app.core import views as core
from app.imports import views as imports
from app.people import views as people
from app.scheduling import exchange_views, feed_views, solve_views, suggest_views
from app.scheduling import views as scheduling

urlpatterns = [
    path("", core.HomeView.as_view(), name="home"),
    path("accounts/", include("allauth.urls")),
    path("django-admin/", admin.site.urls),
    # Organization configuration
    path("settings/", core.OrganizationSettingsView.as_view(), name="organization_settings"),
    path("settings/locations/", core.LocationListView.as_view(), name="locations"),
    path("settings/locations/add/", core.LocationCreateView.as_view(), name="location_add"),
    path("settings/locations/<int:pk>/", core.LocationUpdateView.as_view(), name="location_edit"),
    path("settings/locations/<int:location_pk>/hours/add/", core.HoursCreateView.as_view(), name="hours_add"),
    path(
        "settings/locations/<int:location_pk>/hours/<int:pk>/",
        core.HoursUpdateView.as_view(),
        name="hours_edit",
    ),
    path(
        "settings/locations/<int:location_pk>/hours/<int:pk>/remove/",
        core.HoursDeleteView.as_view(),
        name="hours_delete",
    ),
    path(
        "settings/locations/<int:location_pk>/closures/add/",
        core.ClosureCreateView.as_view(),
        name="closure_add",
    ),
    path(
        "settings/locations/<int:location_pk>/closures/<int:pk>/",
        core.ClosureUpdateView.as_view(),
        name="closure_edit",
    ),
    path(
        "settings/locations/<int:location_pk>/closures/<int:pk>/remove/",
        core.ClosureDeleteView.as_view(),
        name="closure_delete",
    ),
    path("settings/terms/", core.TermListView.as_view(), name="terms"),
    path("settings/terms/add/", core.TermCreateView.as_view(), name="term_add"),
    path("settings/terms/<int:pk>/", core.TermUpdateView.as_view(), name="term_edit"),
    path("settings/departments/", core.DepartmentListView.as_view(), name="departments"),
    path("settings/departments/add/", core.DepartmentCreateView.as_view(), name="department_add"),
    path("settings/departments/<int:pk>/", core.DepartmentUpdateView.as_view(), name="department_edit"),
    path("settings/members/", core.MemberListView.as_view(), name="members"),
    path("settings/audit/", core.AuditLogView.as_view(), name="audit_log"),
    path("settings/retention/", core.RetentionView.as_view(), name="retention"),
    path("settings/pay-roles/", people.PayRoleListView.as_view(), name="pay_roles"),
    path("settings/pay-roles/add/", people.PayRoleCreateView.as_view(), name="pay_role_add"),
    path("settings/pay-roles/<int:pk>/", people.PayRoleUpdateView.as_view(), name="pay_role_edit"),
    path("settings/tags/", people.TagListView.as_view(), name="tags"),
    path("settings/tags/add/", people.TagCreateView.as_view(), name="tag_add"),
    path("settings/tags/<int:pk>/", people.TagUpdateView.as_view(), name="tag_edit"),
    # People
    path("people/", people.EmployeeListView.as_view(), name="employees"),
    path("people/add/", people.EmployeeCreateView.as_view(), name="employee_add"),
    path("people/<int:pk>/", people.EmployeeDetailView.as_view(), name="employee_detail"),
    path("people/<int:pk>/edit/", people.EmployeeUpdateView.as_view(), name="employee_edit"),
    path("people/<int:pk>/archive/", people.EmployeeArchiveView.as_view(), name="employee_archive"),
    path("people/<int:pk>/invite/", people.EmployeeInviteView.as_view(), name="employee_invite"),
    path("people/<int:pk>/records.json", people.EmployeeExportView.as_view(), name="employee_export"),
    path("people/<int:pk>/erase/", people.EmployeeEraseView.as_view(), name="employee_erase"),
    path(
        "people/<int:pk>/remove-from-pool/",
        suggest_views.RemoveFromPoolView.as_view(),
        name="remove_from_pool",
    ),
    path(
        "people/<int:pk>/availability/",
        availability.EmployeeAvailabilityView.as_view(),
        name="employee_availability",
    ),
    # Availability
    path("availability/", availability.MyAvailabilityView.as_view(), name="my_availability"),
    path("time-off/", availability.MyTimeOffView.as_view(), name="my_time_off"),
    path("shifts/exchange/", exchange_views.ShiftExchangeView.as_view(), name="shift_exchange"),
    path("schedule/time-off/", availability.TimeOffReviewView.as_view(), name="time_off_review"),
    path("my-schedule/calendar/", feed_views.CalendarFeedManageView.as_view(), name="calendar_feed_manage"),
    path("calendar/<str:token>.ics", feed_views.CalendarFeedView.as_view(), name="calendar_feed"),
    path("availability/grid/", availability.AvailabilityGridView.as_view(), name="availability_grid"),
    path(
        "availability/reminders/",
        availability.AvailabilityRemindersView.as_view(),
        name="availability_reminders",
    ),
    path(
        "availability/<int:pk>/confirm/",
        availability.ConfirmAvailabilityView.as_view(),
        name="availability_confirm",
    ),
    # Imports
    path("imports/", imports.ImportListView.as_view(), name="imports"),
    path("imports/formats/", imports.ImportFormatsView.as_view(), name="import_formats"),
    path("imports/<int:pk>/", imports.ImportDetailView.as_view(), name="import_detail"),
    # Scheduling
    path("schedule/", scheduling.ScheduleIndexView.as_view(), name="schedule_index"),
    path("schedule/review/", scheduling.ReviewQueueView.as_view(), name="review_queue"),
    path("schedule/<int:period_pk>/", scheduling.PeriodView.as_view(), name="period"),
    path("schedule/<int:period_pk>/day/<str:day>/", scheduling.DayView.as_view(), name="period_day"),
    path("schedule/<int:period_pk>/shifts/add/", scheduling.ShiftCreateView.as_view(), name="shift_add"),
    path(
        "schedule/<int:period_pk>/shifts/<int:pk>/", scheduling.ShiftUpdateView.as_view(), name="shift_edit"
    ),
    path(
        "schedule/<int:period_pk>/shifts/<int:pk>/remove/",
        scheduling.ShiftDeleteView.as_view(),
        name="shift_delete",
    ),
    path(
        "schedule/<int:period_pk>/shifts/<int:pk>/call-out/",
        scheduling.CallOutView.as_view(),
        name="call_out",
    ),
    path(
        "schedule/<int:period_pk>/shifts/<int:pk>/takers/",
        suggest_views.ShiftTakersView.as_view(),
        name="shift_takers",
    ),
    path("schedule/<int:period_pk>/cover/", suggest_views.GapTakersView.as_view(), name="gap_takers"),
    path("schedule/<int:period_pk>/move/", suggest_views.TargetedMoveView.as_view(), name="targeted_move"),
    path(
        "schedule/<int:period_pk>/shifts/<int:pk>/split/",
        suggest_views.SplitShiftView.as_view(),
        name="split_shift",
    ),
    path(
        "schedule/<int:period_pk>/raise/<int:employee_pk>/",
        suggest_views.RaiseHoursView.as_view(),
        name="raise_hours",
    ),
    path("schedule/<int:period_pk>/publish/", scheduling.PublishView.as_view(), name="publish"),
    path("schedule/<int:period_pk>/reports/", scheduling.ReportsView.as_view(), name="reports"),
    path("schedule/<int:period_pk>/versions/", scheduling.VersionListView.as_view(), name="versions"),
    path(
        "schedule/<int:period_pk>/versions/<int:number>/",
        scheduling.VersionDetailView.as_view(),
        name="version_detail",
    ),
    path("schedule/<int:period_pk>/discard/", scheduling.DiscardDraftsView.as_view(), name="discard_drafts"),
    path("schedule/<int:period_pk>/solve/", solve_views.SolveStartView.as_view(), name="solve_start"),
    path("schedule/<int:period_pk>/solve/<int:pk>/", solve_views.SolveRunView.as_view(), name="solve_run"),
    path(
        "schedule/<int:period_pk>/solve/<int:pk>/what-if/", solve_views.WhatIfView.as_view(), name="what_if"
    ),
    path("settings/rules/", scheduling.RulesView.as_view(), name="rules"),
    path("availability/desired/", scheduling.DesiredHoursSubmitView.as_view(), name="desired_submit"),
    path("schedule/desired-hours/", scheduling.DesiredHoursReviewView.as_view(), name="desired_hours"),
    path("notifications/", scheduling.NotificationsView.as_view(), name="notifications"),
    path("notifications/email/", scheduling.EmailSettingsView.as_view(), name="email_settings"),
    path("my-schedule/", scheduling.MyScheduleView.as_view(), name="my_schedule"),
    path("published/", scheduling.PublishedScheduleView.as_view(), name="published_schedule"),
    path(
        "schedule/<int:period_pk>/versions/<int:number>/export.csv",
        scheduling.VersionExportView.as_view(),
        name="version_export",
    ),
    path(
        "schedule/<int:period_pk>/versions/<int:number>/print/",
        scheduling.VersionPrintView.as_view(),
        name="version_print",
    ),
]

admin.site.site_header = "Stacks administration"
