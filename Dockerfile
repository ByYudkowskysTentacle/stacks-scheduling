# SPDX-License-Identifier: AGPL-3.0-or-later
# One image for web, worker and migrate (ADR 0001). `--target dev` adds test tools.
FROM python:3.13-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
RUN useradd --create-home --uid 1000 app
WORKDIR /srv

FROM base AS prod
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN DJANGO_SECRET_KEY=collectstatic-only POSTGRES_PASSWORD=unused python manage.py collectstatic --noinput
USER app
EXPOSE 8000
CMD ["gunicorn", "app.config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--access-logformat", "%(m)s %(U)s %(s)s %(L)ss"]

FROM base AS dev
# git for tools/check_sensitive.py; libatomic1 for the Node that pyright downloads.
# safe.directory: the bind-mounted checkout is often owned by another uid than app.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git libatomic1 \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory /srv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project
COPY . .
USER app
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
