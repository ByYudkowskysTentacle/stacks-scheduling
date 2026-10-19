# SPDX-License-Identifier: AGPL-3.0-or-later
"""Settings for the test suite: production settings with test-only defaults filled in."""

import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-secret-key")
os.environ.setdefault("POSTGRES_PASSWORD", "stacks")
os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,localhost")

from app.config.settings import *  # noqa: F403

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]  # fast hashing in tests only
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
