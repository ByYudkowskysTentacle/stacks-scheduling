# SPDX-License-Identifier: AGPL-3.0-or-later
from django.contrib import admin
from django.urls import include, path

from app.core import views as core
from app.people import views as people

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
    path("settings/members/", core.MemberListView.as_view(), name="members"),
    path("settings/audit/", core.AuditLogView.as_view(), name="audit_log"),
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
]

admin.site.site_header = "Stacks administration"
