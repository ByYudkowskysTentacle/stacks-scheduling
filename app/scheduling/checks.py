# SPDX-License-Identifier: AGPL-3.0-or-later
"""Start-up checks for phone notifications (D125): a VAPID key that is set but unusable turns
push off, so say so in `manage.py check` and the container logs rather than failing quietly."""

from __future__ import annotations

import re

from django.conf import settings
from django.core.checks import Warning, register

from adapters import webpush

_HOST = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}$")


@register()
def push_settings(app_configs, **kwargs):
    key = settings.STACKS_VAPID_PRIVATE_KEY
    if not key:
        return []
    problems = []
    try:
        webpush.private_key_from_text(key)
    except ValueError:
        problems.append(
            Warning(
                "STACKS_VAPID_PRIVATE_KEY isn't a usable key, so phone notifications are off.",
                hint="Use an unencrypted P-256 key, or make a new one with `python manage.py vapid_keys`.",
                id="stacks.W001",
            )
        )
    if not webpush.subject_ok(settings.STACKS_VAPID_SUBJECT):
        problems.append(
            Warning(
                "STACKS_VAPID_SUBJECT must be a mailto: or https: address that isn't localhost, "
                "so phone notifications are off.",
                hint="Set it to IT's email, for example mailto:it@library.example.edu, or set STACKS_SITE_URL.",
                id="stacks.W002",
            )
        )
    if settings.STACKS_PUSH_TTL < 0:
        problems.append(
            Warning(
                "STACKS_PUSH_TTL can't be negative, so phone notifications are off.",
                hint="It's how many seconds a push service may hold a message (default 259200, 3 days).",
                id="stacks.W004",
            )
        )
    for entry in settings.STACKS_PUSH_EXTRA_HOSTS:
        if not _HOST.match(webpush.host_entry(entry)):
            problems.append(
                Warning(
                    f"STACKS_PUSH_EXTRA_HOSTS should list host names only, such as push.example.org, not {entry!r}.",
                    id="stacks.W003",
                )
            )
    return problems
