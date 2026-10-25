"""Typed readers for environment variables (12-factor configuration).

Settings modules use these instead of os.environ directly so that a missing
required value fails at start-up with a clear message, not at first use.
"""

import os

from django.core.exceptions import ImproperlyConfigured

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}
_UNSET = object()


def env_str(name: str, default=_UNSET) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        if default is _UNSET:
            raise ImproperlyConfigured(f"The {name} environment variable is required.")
        return default
    return value


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ImproperlyConfigured(f"{name} must be a boolean (1/0, true/false), got {value!r}.")


def env_int(name: str, default=_UNSET) -> int:
    value = env_str(name, None if default is _UNSET else str(default))
    if value is None:
        raise ImproperlyConfigured(f"The {name} environment variable is required.")
    try:
        return int(value)
    except ValueError:
        raise ImproperlyConfigured(f"{name} must be an integer, got {value!r}.") from None


def env_list(name: str, default=_UNSET) -> list[str]:
    """Comma-separated list; blank entries are dropped."""
    value = env_str(name, None if default is _UNSET else ",".join(default))
    if value is None:
        raise ImproperlyConfigured(f"The {name} environment variable is required.")
    return [item.strip() for item in value.split(",") if item.strip()]
