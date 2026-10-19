# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real-browser tests (Playwright, Chromium). Skipped when no browser is installed.

CI installs one with `uv run playwright install --with-deps chromium`. Locally, set
CHROMIUM_PATH to an existing Chromium binary if Playwright's own download is absent.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")  # Playwright's sync API runs an event loop


@pytest.fixture(scope="session")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
        except Exception as exc:  # no browser available on this machine
            if os.environ.get("REQUIRE_BROWSER"):
                raise  # CI sets this, so browser tests can't pass by being skipped
            pytest.skip(f"Chromium not available: {exc.__class__.__name__}")
        yield b
        b.close()
