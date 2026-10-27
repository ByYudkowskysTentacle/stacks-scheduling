# SPDX-License-Identifier: AGPL-3.0-or-later
"""Django settings. Everything deployment-specific comes from the environment (CLAUDE.md §6).

See `.env.example` for the variables. Secrets may also be given as files: for any
variable `X`, setting `X_FILE` to a path reads the value from that file (Docker secrets).
"""

from __future__ import annotations

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def env(name: str, default: str | None = None) -> str:
    file_path = os.environ.get(f"{name}_FILE")
    if file_path:
        return Path(file_path).read_text(encoding="utf-8").strip()
    value = os.environ.get(name, default)
    if value is None:
        raise ImproperlyConfigured(f"Set the {name} environment variable (see .env.example).")
    return value


def env_bool(name: str, default: bool = False) -> bool:
    return env(name, "1" if default else "0").lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    return [v.strip() for v in env(name, default).split(",") if v.strip()]


DEBUG = env_bool("DJANGO_DEBUG")
SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-only-insecure-key" if DEBUG else None)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1" if DEBUG else "")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.forms",  # Django's own form templates, which ours in app/templates/django/forms extend
    "django.contrib.humanize",
    "pgtrigger",
    "pghistory",
    "pghistory.admin",
    "allauth",
    "allauth.account",
    "allauth.mfa",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.openid_connect",
    "procrastinate.contrib.django",
    "app.accounts",
    "app.core",
    "app.people",
    "app.availability",
    "app.imports",
    "app.scheduling",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "app.core.middleware.OrganizationMiddleware",
    "pghistory.middleware.HistoryMiddleware",
]

ROOT_URLCONF = "app.config.urls"
WSGI_APPLICATION = "app.config.wsgi.application"

# Forms render through project templates (app/templates/django/forms/): labels without colons,
# "(optional)" on optional fields, inline checkboxes and "Choose…" for empty selects (audit 2.20).
FORM_RENDERER = "django.forms.renderers.TemplatesSetting"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "app" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "app.core.context_processors.organization",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("POSTGRES_DB", "stacks"),
        "USER": env("POSTGRES_USER", "stacks"),
        "PASSWORD": env("POSTGRES_PASSWORD", "stacks" if DEBUG else None),
        "HOST": env("POSTGRES_HOST", "localhost"),
        "PORT": env("POSTGRES_PORT", "5432"),
        "ATOMIC_REQUESTS": True,
        "CONN_MAX_AGE": 60,
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_USER_MODEL = "accounts.User"
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Local accounts (SPEC §11). People are invited by a supervisor; there is no public sign-up.
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]
ACCOUNT_USER_MODEL_USERNAME_FIELD = None
ACCOUNT_EMAIL_VERIFICATION = "none"
ACCOUNT_ADAPTER = "app.accounts.adapter.InviteOnlyAccountAdapter"
ACCOUNT_LOGIN_ON_PASSWORD_RESET = False
ACCOUNT_PASSWORD_RESET_BY_CODE_ENABLED = False
ACCOUNT_LOGOUT_ON_PASSWORD_CHANGE = False
ACCOUNT_RATE_LIMITS = {"login_failed": "10/m/ip,5/5m/key"}
MFA_SUPPORTED_TYPES = ["totp", "recovery_codes"]
MFA_TOTP_ISSUER = env("STACKS_SITE_NAME", "Stacks")
# Campus single sign-on over OpenID Connect (SPEC §11, D5, D118). Off unless OIDC_CLIENT_ID is
# set. Google Workspace and any other OIDC provider (Entra, Okta, Keycloak) are configuration.
# Only people who already have a Stacks account can sign in this way: it never creates one.
OIDC_CLIENT_ID = env("OIDC_CLIENT_ID", "")
OIDC_ALLOWED_DOMAINS = [d.lower() for d in env_list("OIDC_ALLOWED_DOMAINS")]
OIDC_REQUIRE_HD = env_bool("OIDC_REQUIRE_HD")  # Google: the account must belong to the Workspace
SOCIALACCOUNT_ADAPTER = "app.accounts.adapter.CampusSSOAdapter"
SOCIALACCOUNT_EMAIL_AUTHENTICATION = False  # the adapter links accounts itself, after its checks
SOCIALACCOUNT_STORE_TOKENS = False
SOCIALACCOUNT_PROVIDERS: dict = {}
if OIDC_CLIENT_ID:
    if not OIDC_ALLOWED_DOMAINS:
        raise ImproperlyConfigured("Set OIDC_ALLOWED_DOMAINS when OIDC_CLIENT_ID is set (see .env.example).")
    _oidc_params = {"prompt": "select_account"}
    if OIDC_REQUIRE_HD and len(OIDC_ALLOWED_DOMAINS) == 1:
        _oidc_params["hd"] = OIDC_ALLOWED_DOMAINS[0]  # Google's account picker hint; checked again on return
    SOCIALACCOUNT_PROVIDERS["openid_connect"] = {
        "OAUTH_PKCE_ENABLED": True,
        "APPS": [
            {
                "provider_id": env("OIDC_PROVIDER_ID", "campus"),
                "name": env("OIDC_PROVIDER_NAME", "campus sign-in"),
                "client_id": OIDC_CLIENT_ID,
                "secret": env("OIDC_CLIENT_SECRET"),
                "settings": {"server_url": env("OIDC_SERVER_URL"), "auth_params": _oidc_params},
            }
        ],
    }

LOGIN_URL = "account_login"
LOGIN_REDIRECT_URL = "home"
ACCOUNT_LOGOUT_REDIRECT_URL = "account_login"

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("STACKS_TIME_ZONE", "America/Chicago")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "app" / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
        if not DEBUG
        else "django.contrib.staticfiles.storage.StaticFilesStorage"
    },
}

# Email over SMTP from the environment (D14). Development prints to the console.
EMAIL_BACKEND = env(
    "DJANGO_EMAIL_BACKEND",
    "django.core.mail.backends.console.EmailBackend"
    if DEBUG
    else "django.core.mail.backends.smtp.EmailBackend",
)
EMAIL_HOST = env("SMTP_HOST", "localhost")
EMAIL_PORT = int(env("SMTP_PORT", "587"))
EMAIL_HOST_USER = env("SMTP_USER", "")
EMAIL_HOST_PASSWORD = env("SMTP_PASSWORD", "")
EMAIL_USE_TLS = env_bool("SMTP_USE_TLS", True)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "stacks@localhost")

# Security. Caddy terminates TLS and forwards the scheme.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 60 * 60 * 12
CSRF_COOKIE_HTTPONLY = True
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(env("DJANGO_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = False

# Logging: never log personal data (CLAUDE.md §2.1). Request logs carry method, path and status only,
# and Django's error emails to admins are off because they include request details.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": env("DJANGO_LOG_LEVEL", "INFO")},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "django.security": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}
ADMINS: list[tuple[str, str]] = []

STACKS_SITE_NAME = env("STACKS_SITE_NAME", "Stacks")
# Absolute links in emails sent by the worker, which has no request to read the host from.
STACKS_SITE_URL = env(
    "STACKS_SITE_URL", f"https://{os.environ['SITE_ADDRESS']}" if os.environ.get("SITE_ADDRESS") else ""
)

# Solver (ADR 0001, SPEC §7.3.2). Workers default to the CPUs given to the worker container.
STACKS_SOLVER_WORKERS = int(env("STACKS_SOLVER_WORKERS", env("WORKER_CPUS", "4")))
STACKS_GENERATE_SECONDS = int(env("STACKS_GENERATE_SECONDS", "300"))
STACKS_REBALANCE_SECONDS = int(env("STACKS_REBALANCE_SECONDS", "30"))
STACKS_SOLVE_INLINE = env_bool("STACKS_SOLVE_INLINE")  # run solves in the request (tests only)
