# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stacks as an installable web app on phones (D124): the web app manifest, the service worker,
the offline page and the "put it on your phone" help. None of these need signing in, and none
carry anyone's data: the service worker saves a person's own pages on their own device only as
they view them, and drops them when they sign out (D122)."""

from __future__ import annotations

import hashlib
import json

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
from django.templatetags.static import static
from django.urls import reverse
from django.views import View

from app.scheduling import push

THEME_COLOUR = "#12434f"  # --header in stacks.css
BACKGROUND_COLOUR = "#fbfaf7"  # --paper

# Files the service worker keeps for offline use, so a saved page still looks right.
PRECACHE_STATIC = [
    "css/stacks.css",
    "fonts/atkinson-hyperlegible-latin-400-normal.woff2",
    "fonts/atkinson-hyperlegible-latin-700-normal.woff2",
    "js/install.js",
    "js/scroll-regions.js",
    "js/print-details.js",
    "icons/icon-192.png",
    "icons/icon.svg",
]

# Pages saved as the person views them, so they can be read without signal: their home page,
# their own shifts and the published schedule.
SAVED_PAGES = ["home", "my_schedule", "published_schedule"]


class ManifestView(View):
    def get(self, request):
        name = settings.STACKS_SITE_NAME
        manifest = {
            "id": reverse("home"),
            "name": name,
            "short_name": name[:12],
            "description": "Your shifts, availability and the library's published schedule.",
            "start_url": reverse("home"),
            "scope": "/",
            "display": "standalone",
            "theme_color": THEME_COLOUR,
            "background_color": BACKGROUND_COLOUR,
            "icons": [
                {"src": static("icons/icon-192.png"), "sizes": "192x192", "type": "image/png"},
                {"src": static("icons/icon-512.png"), "sizes": "512x512", "type": "image/png"},
                {
                    "src": static("icons/icon-maskable-512.png"),
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "maskable",
                },
            ],
        }
        response = JsonResponse(manifest, content_type="application/manifest+json")
        response["Cache-Control"] = "public, max-age=86400"
        return response


class ServiceWorkerView(View):
    """Served from the site root so it can look after every page (a worker's reach is the
    folder it's served from). Browsers check it for changes on each visit; `version` changes
    whenever a static file's hashed name does, which replaces the offline copies."""

    def get(self, request):
        precache = [reverse("offline"), *(static(path) for path in PRECACHE_STATIC)]
        config = {
            "version": hashlib.sha256(" ".join(precache).encode()).hexdigest()[:12],
            "precache": precache,
            "staticPrefix": static(""),
            "offlineUrl": reverse("offline"),
            "savedPages": [reverse(name) for name in SAVED_PAGES],
            "loginPath": reverse("account_login"),
            "logoutPath": reverse("account_logout"),
            "siteName": settings.STACKS_SITE_NAME,
            "icon": static("icons/icon-192.png"),
            "badge": static("icons/icon-badge-96.png"),
            # Phone notifications (D125), or None when the site has no VAPID key.
            "push": {
                "publicKey": push.public_key(),
                "dropUrl": reverse("push_drop"),
                "renewUrl": reverse("push_renew"),
            }
            if push.enabled()
            else None,
        }
        response = render(
            request, "sw.js", {"config": json.dumps(config)}, content_type="text/javascript; charset=utf-8"
        )
        response["Cache-Control"] = "no-cache"
        return response


class OfflineView(View):
    def get(self, request):
        return render(request, "core/offline.html", {"nav": None})  # the same copy for anyone


class InstallHelpView(View):
    """Public, so a poster or an email can point here before anyone has signed in."""

    def get(self, request):
        return render(request, "core/install.html")
