# syntax=docker/dockerfile:1

# One image, two targets:
#   dev         Docker Compose: dev dependencies, runserver, source bind-mounted
#   production  Gunicorn, static files collected, no dev tooling (the default)
ARG PYTHON_VERSION=3.13

FROM python:${PYTHON_VERSION}-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv
RUN groupadd --system app && useradd --system --gid app --home-dir /app app
WORKDIR /app

FROM base AS deps
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project

FROM deps AS dev
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-install-project
COPY backend ./backend
WORKDIR /app/backend
USER app
ENV DJANGO_SETTINGS_MODULE=config.settings.local
EXPOSE 8000
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]

FROM deps AS production
COPY backend ./backend
WORKDIR /app/backend
# collectstatic imports the production settings, which refuse to load without
# these. They are build-time placeholders, not secrets; real values are
# injected at runtime from the platform's secret store.
RUN DJANGO_SECRET_KEY=collectstatic-placeholder \
    DJANGO_ALLOWED_HOSTS=localhost \
    DJANGO_CSRF_TRUSTED_ORIGINS=http://localhost \
    SITE_URL=http://localhost \
    python manage.py collectstatic --noinput --settings=config.settings.production
USER app
ENV DJANGO_SETTINGS_MODULE=config.settings.production
EXPOSE 8000
# No --access-logfile: Gunicorn would log full paths, including password-reset
# and verification tokens (threat model T12).
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--threads", "2", \
     "--timeout", "30", "--max-requests", "1000", "--max-requests-jitter", "100"]
