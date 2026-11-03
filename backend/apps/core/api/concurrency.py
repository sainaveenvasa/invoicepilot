"""Optimistic concurrency headers (API contracts §1.4)."""

import re

from rest_framework.response import Response

from apps.core import errors
from apps.core.api.errors import PROBLEM_CONTENT_TYPE, problem

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


def version_conflict_response(conflict, current_data: dict) -> Response:
    """412 with the record as it is now, so the client can merge and retry (API contracts §1.4)."""
    body = problem(conflict.status, conflict.code, conflict.detail, current=current_data)
    headers = {"ETag": etag(conflict.current.version)}
    return Response(body, status=conflict.status, content_type=PROBLEM_CONTENT_TYPE, headers=headers)
