"""Settings shared by every environment.

Environment modules (local, test, production) import this and override only
what differs. All configuration comes from environment variables; secrets are
never written here (system design §12.2, threat model T10).
"""

from datetime import timedelta
from pathlib import Path

from config.env import env_bool, env_int, env_list, env_str

BASE_DIR = Path(__file__).resolve().parent.parent.parent  # backend/

# Off unless an environment module turns it on. Production never does.
DEBUG = False

# Each environment module sets SECRET_KEY; production requires it from the environment.
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", [])
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", [])

# Absolute origin used to build links in emails (verification, password
# reset). Never derived from the request's Host header (threat model T4).
SITE_URL = env_str("SITE_URL", "http://localhost:8000").rstrip("/")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "apps.core",
    "apps.accounts",
    "apps.organizations",
    "apps.customers",
    "apps.invoices",
    "apps.imports",
    "apps.reminders",
    "apps.billing",
    "apps.audit",
    "apps.webhooks",
    "apps.retention",
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
    "apps.organizations.middleware.TenantMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# PostgreSQL is required: the schema relies on composite foreign keys,
# partial unique indexes and triggers. SQLite is not supported.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env_str("POSTGRES_DB", "invoicepilot"),
        "USER": env_str("POSTGRES_USER", "invoicepilot"),
        "PASSWORD": env_str("POSTGRES_PASSWORD", ""),
        "HOST": env_str("POSTGRES_HOST", "localhost"),
        "PORT": env_str("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": env_int("POSTGRES_CONN_MAX_AGE", 60),
        "CONN_HEALTH_CHECKS": True,
    }
}

# Shared across processes, so login throttling counts every worker's
# attempts. Create the table with `manage.py createcachetable`. Swap for
# Redis when the broker is introduced (system design §1).
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "django_cache",
    }
}

AUTH_USER_MODEL = "accounts.User"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Authentication -------------------------------------------------------

AUTHENTICATION_BACKENDS = ["django.contrib.auth.backends.ModelBackend"]

# Argon2id first; PBKDF2 stays so older hashes are upgraded on login.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.ScryptPasswordHasher",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "app:dashboard"
LOGOUT_REDIRECT_URL = "accounts:login"

PASSWORD_RESET_TIMEOUT = 3600  # one hour (threat model T4)
EMAIL_VERIFICATION_MAX_AGE = timedelta(hours=48)

# Throttles (threat model T4): (attempts, window in seconds).
LOGIN_THROTTLE_PER_ACCOUNT = (5, 15 * 60)
LOGIN_THROTTLE_PER_IP = (30, 10 * 60)
SIGNUP_THROTTLE_PER_IP = (5, 60 * 60)
PASSWORD_RESET_THROTTLE_PER_IP = (5, 60 * 60)
VERIFICATION_RESEND_THROTTLE_PER_USER = (3, 60 * 60)
# Proxies in front of the app that append to X-Forwarded-For. 0 means use
# REMOTE_ADDR; behind one load balancer, set 1, or every client shares the
# balancer's address and IP throttles lock everyone out together.
NUM_TRUSTED_PROXIES = env_int("DJANGO_NUM_TRUSTED_PROXIES", 0)

# --- Sessions, cookies and headers ---------------------------------------

SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_AGE = 14 * 24 * 60 * 60
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
# Readable by JavaScript on purpose: API clients copy it into X-CSRFToken.
CSRF_COOKIE_HTTPONLY = False
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# --- Email ----------------------------------------------------------------

EMAIL_BACKEND = env_str("EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = env_str("EMAIL_HOST", "localhost")
EMAIL_PORT = env_int("EMAIL_PORT", 25)
EMAIL_HOST_USER = env_str("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env_str("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", False)
EMAIL_TIMEOUT = 10
DEFAULT_FROM_EMAIL = env_str("DEFAULT_FROM_EMAIL", "InvoicePilot <no-reply@localhost>")

# --- API ------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.SessionAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
        "apps.organizations.api.permissions.IsWorkspaceMember",
    ],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],  # no browsable API
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_PAGINATION_CLASS": "apps.core.api.pagination.CursorPagination",
    "EXCEPTION_HANDLER": "apps.core.api.errors.problem_exception_handler",
}

# --- Internationalization -------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# --- Static files ---------------------------------------------------------

STATIC_URL = "static/"
# Vendored front-end assets (HTMX), served by WhiteNoise; never from a CDN.
STATICFILES_DIRS = [BASE_DIR / "static"]

# --- Logging --------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": env_str("DJANGO_LOG_LEVEL", "INFO")},
}
