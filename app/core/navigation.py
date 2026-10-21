# SPDX-License-Identifier: AGPL-3.0-or-later
"""The main navigation, built once per request and drawn twice: as the header nav on wide
screens and as the bottom tab bar on phones (D114). Which links appear follows the same
permissions as the views; a link here never grants access."""

from __future__ import annotations

from dataclasses import dataclass

from django.urls import reverse

from app.core.permissions import Perm

TAB_BAR_SLOTS = 4  # plus "More" on phones

REVIEW_PAGES = {"review_queue", "time_off_review", "desired_hours"}


@dataclass(frozen=True)
class NavItem:
    key: str
    label: str
    url: str
    icon: str
    current: bool
    short: str = ""  # a shorter label for the phone tab bar
    tab_priority: int | None = None  # lower comes first; None never goes on the tab bar
    count: int = 0  # unread notifications

    @property
    def tab_label(self) -> str:
        return self.short or self.label


@dataclass(frozen=True)
class Navigation:
    items: list[NavItem]
    tabs: list[NavItem]
    more: list[NavItem]

    @property
    def more_current(self) -> bool:
        return any(i.current for i in self.more)

    @property
    def more_count(self) -> int:
        return sum(i.count for i in self.more)


def build(request, unread: int = 0) -> Navigation | None:
    access = getattr(request, "access", None)
    if access is None or not request.user.is_authenticated:
        return None
    match = request.resolver_match
    name = (match.url_name or "") if match else ""
    path = request.path
    planner = access.has(Perm.VIEW_DRAFTS)
    items: list[NavItem] = [NavItem("home", "Home", reverse("home"), "home", name == "home", tab_priority=0)]
    if planner:
        on_schedule = (
            path.startswith("/schedule/") and name not in REVIEW_PAGES
        ) or name == "published_schedule"
        items.append(
            NavItem(
                "schedule", "Schedule", reverse("schedule_index"), "calendar", on_schedule, tab_priority=1
            )
        )
    else:
        items.append(
            NavItem(
                "schedule",
                "Schedule",
                reverse("published_schedule"),
                "calendar",
                name == "published_schedule",
            )
        )
    if access.has(Perm.EDIT_SCHEDULE):
        items.append(
            NavItem(
                "review", "Review", reverse("review_queue"), "checklist", name in REVIEW_PAGES, tab_priority=2
            )
        )
    if access.employee:
        items += [
            NavItem(
                "my_schedule",
                "My schedule",
                reverse("my_schedule"),
                "calendar-check",
                name in {"my_schedule", "calendar_feed_manage"},
                short="My shifts",
                tab_priority=4,
            ),
            NavItem(
                "availability",
                "My availability",
                reverse("my_availability"),
                "clock",
                name == "my_availability",
                short="Availability",
                tab_priority=5,
            ),
            NavItem(
                "open_shifts",
                "Open shifts",
                reverse("shift_exchange"),
                "swap",
                name in {"shift_exchange", "shift_swap"},
                tab_priority=6,
            ),
            NavItem("time_off", "Time off", reverse("my_time_off"), "calendar-off", name == "my_time_off"),
        ]
    if access.has(Perm.VIEW_FREE_BUSY):
        items.append(
            NavItem(
                "availability_grid",
                "Availability grid",
                reverse("availability_grid"),
                "grid",
                name in {"availability_grid", "availability_reminders"},
            )
        )
    if access.is_supervisor:
        on_people = "employee" in name or "import" in name
        items.append(NavItem("people", "People", reverse("employees"), "users", on_people, tab_priority=3))
    if access.has(Perm.CONFIGURE):
        items.append(
            NavItem(
                "settings",
                "Settings",
                reverse("organization_settings"),
                "settings",
                path.startswith("/settings/"),
            )
        )
    items += [
        NavItem(
            "notifications",
            "Notifications",
            reverse("notifications"),
            "bell",
            name in {"notifications", "email_settings"},
            count=unread,
        ),
        NavItem(
            "account",
            "Account",
            reverse("account_change_password"),
            "user",
            name.startswith(("account_", "mfa_")),
        ),
    ]
    ranked = sorted((i for i in items if i.tab_priority is not None), key=lambda i: i.tab_priority or 0)
    tabs = ranked[:TAB_BAR_SLOTS]
    more = [i for i in items if i not in tabs]
    return Navigation(items=items, tabs=tabs, more=more)
