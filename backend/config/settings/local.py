"""Developer machines and Docker Compose. Never used in a deployed environment."""

from config.env import env_bool, env_list, env_str

from .base import *  # noqa: F403

DEBUG = env_bool("DJANGO_DEBUG", True)

# A fixed key is fine here: local sessions and tokens protect nothing real.
SECRET_KEY = env_str("DJANGO_SECRET_KEY", "local-dev-only-insecure-key")  # noqa: S105

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", ["localhost", "127.0.0.1", "[::1]"])

# Print emails to the console unless Compose points us at Mailpit.
EMAIL_BACKEND = env_str("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
