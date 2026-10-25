"""Fixed-window attempt counters in the shared cache (threat model T4).

Identifiers are hashed before they become cache keys, so email addresses and
IP addresses never appear in the cache.
"""

import hashlib

from django.conf import settings
from django.core.cache import cache


def _key(scope: str, ident: str) -> str:
    digest = hashlib.sha256(ident.encode()).hexdigest()
    return f"throttle:{scope}:{digest}"


def is_limited(scope: str, ident: str, rate: tuple[int, int]) -> bool:
    attempts, _window = rate
    return cache.get(_key(scope, ident), 0) >= attempts


def hit(scope: str, ident: str, rate: tuple[int, int]) -> None:
    """Count one attempt. The window starts at the first attempt."""
    _attempts, window = rate
    key = _key(scope, ident)
    if not cache.add(key, 1, timeout=window):
        try:
            cache.incr(key)
        except ValueError:  # expired between add() and incr()
            cache.add(key, 1, timeout=window)


def reset(scope: str, ident: str) -> None:
    cache.delete(_key(scope, ident))


def client_ip(request) -> str:
    """The client's address. Behind N trusted proxies, the Nth entry from the
    right of X-Forwarded-For; entries further left are client-controlled."""
    proxies = settings.NUM_TRUSTED_PROXIES
    if proxies:
        forwarded = [part.strip() for part in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if part.strip()]
        if len(forwarded) >= proxies:
            return forwarded[-proxies]
    return request.META.get("REMOTE_ADDR", "")
