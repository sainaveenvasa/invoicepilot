"""pytest and CI. Still PostgreSQL: the schema's guarantees are only real there."""

from .base import *  # noqa: F403
from .base import DATABASES

DEBUG = False
SECRET_KEY = "test-only-insecure-key"  # noqa: S105

ALLOWED_HOSTS = ["testserver", "localhost"]

# Deliberately different from the test client's host, so tests can prove
# that emailed links never come from the request's Host header.
SITE_URL = "https://app.invoicepilot.test"

DATABASES["default"]["CONN_MAX_AGE"] = 0

# Argon2 is the production hasher (asserted in tests); MD5 keeps the suite fast.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
