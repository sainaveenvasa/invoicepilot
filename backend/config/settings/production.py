"""Staging and production. Every secret and host comes from the environment,
and start-up fails if one is missing (threat model T10)."""

from config.env import env_bool, env_int, env_list, env_str

from .base import *  # noqa: F403
from .base import BASE_DIR

DEBUG = False

SECRET_KEY = env_str("DJANGO_SECRET_KEY")
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")
SITE_URL = env_str("SITE_URL").rstrip("/")

SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", True)
# Load balancer health checks arrive over plain HTTP.
SECURE_REDIRECT_EXEMPT = [r"^healthz$", r"^readyz$"]
SECURE_HSTS_SECONDS = env_int("DJANGO_SECURE_HSTS_SECONDS", 31536000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", False)
SECURE_HSTS_PRELOAD = env_bool("DJANGO_SECURE_HSTS_PRELOAD", False)
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# Only behind a proxy that always overwrites this header; otherwise a client
# could claim HTTPS.
if env_bool("DJANGO_SECURE_PROXY_SSL_HEADER", False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Subdomains and preload are switched on once HTTPS is stable everywhere
# (system design §12.2), so `check --deploy` stays clean until then.
SILENCED_SYSTEM_CHECKS = ["security.W005", "security.W021"]

# Filled by `collectstatic` at image build time and served by WhiteNoise.
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
