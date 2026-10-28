"""Optimistic concurrency headers (API contracts §1.4)."""

import re

from apps.core import errors

_ENTITY_TAG = re.compile(r'"(\d{1,10})"')


def etag(version: int) -> str:
    return f'"{version}"'


def if_match_version(request) -> int | None:
    """The version from ``If-Match: "<version>"``. Missing → 428. A value that
    isn't one of our tags returns None, which never matches, so it gets 412."""
    header = request.headers.get("If-Match")
    if header is None:
        raise errors.PreconditionRequired()
    match = _ENTITY_TAG.fullmatch(header.strip())
    return int(match.group(1)) if match else None
