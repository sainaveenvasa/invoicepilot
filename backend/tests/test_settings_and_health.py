"""Configuration safety (threat model T3, T4, T10) and the health endpoints."""

import os
import secrets
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
from django.db import OperationalError
from django.urls import reverse

from config.settings import base

BACKEND_DIR = Path(__file__).resolve().parents[1]

PRODUCTION_ENV = {
    "DJANGO_ALLOWED_HOSTS": "app.invoicepilot.example",
    "DJANGO_CSRF_TRUSTED_ORIGINS": "https://app.invoicepilot.example",
    "SITE_URL": "https://app.invoicepilot.example",
}


def run_with_production_settings(args, **env):
    """Run a fresh interpreter, so the production module is imported with exactly this environment."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith("DJANGO_") and k != "SITE_URL"}
    clean.update(DJANGO_SETTINGS_MODULE="config.settings.production", **env)
    return subprocess.run(  # noqa: S603 - fixed argv, our own interpreter
        [sys.executable, *args], cwd=BACKEND_DIR, env=clean, capture_output=True, text=True, timeout=60
    )


@pytest.mark.parametrize("missing", ["DJANGO_SECRET_KEY", "DJANGO_ALLOWED_HOSTS", "SITE_URL"])
def test_production_settings_refuse_to_start_without_required_values(missing):
    env = {**PRODUCTION_ENV, "DJANGO_SECRET_KEY": secrets.token_urlsafe(50)}
    del env[missing]
    result = run_with_production_settings(["-c", "import django; django.setup()"], **env)
    assert result.returncode != 0
    assert f"The {missing} environment variable is required." in result.stderr


def test_production_settings_pass_deploy_checks():
    result = run_with_production_settings(
        ["manage.py", "check", "--deploy", "--fail-level", "WARNING"],
        DJANGO_SECRET_KEY=secrets.token_urlsafe(50),
        **PRODUCTION_ENV,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_debug_is_off_unless_an_environment_turns_it_on():
    assert base.DEBUG is False


def test_passwords_are_hashed_with_argon2_first():
    import argon2  # noqa: F401 - the hasher's dependency is installed

    assert base.PASSWORD_HASHERS[0] == "django.contrib.auth.hashers.Argon2PasswordHasher"


def test_password_policy_and_session_settings():
    min_length = next(v for v in base.AUTH_PASSWORD_VALIDATORS if v["NAME"].endswith("MinimumLengthValidator"))
    assert min_length["OPTIONS"]["min_length"] == 12
    assert base.PASSWORD_RESET_TIMEOUT == 3600
    assert base.SESSION_COOKIE_HTTPONLY is True
    assert base.SESSION_COOKIE_SAMESITE == "Lax"
    assert base.SESSION_COOKIE_AGE == 14 * 24 * 60 * 60


def test_security_headers_present(client):
    response = client.get(reverse("healthz"))
    assert response["X-Frame-Options"] == "DENY"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["Referrer-Policy"] == "same-origin"


def test_healthz_needs_no_database_or_login(client):
    with mock.patch("apps.core.views.connection") as connection:
        response = client.get(reverse("healthz"))
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    connection.cursor.assert_not_called()


@pytest.mark.django_db
def test_readyz_checks_the_database(client):
    response = client.get(reverse("readyz"))
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_readyz_reports_an_unreachable_database(client):
    with mock.patch("apps.core.views.connection") as connection:
        connection.cursor.side_effect = OperationalError("connection refused")
        response = client.get(reverse("readyz"))
    assert response.status_code == 503
    assert response.json()["database"] == "error"
    assert "connection refused" not in response.content.decode()


def test_health_endpoints_are_read_only(client):
    assert client.post(reverse("healthz")).status_code == 405
