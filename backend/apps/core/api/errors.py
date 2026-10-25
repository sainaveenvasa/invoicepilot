"""RFC 9457 problem documents for every API error (API contracts §1.6)."""

from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import JsonResponse
from rest_framework import exceptions, status
from rest_framework.response import Response
from rest_framework.views import exception_handler

from apps.core.errors import DomainError

PROBLEM_CONTENT_TYPE = "application/problem+json"
PROBLEM_TYPE_BASE = "https://docs.invoicepilot.app/problems/"

TITLES = {
    "validation_error": "The request is not valid.",
    "not_authenticated": "Authentication is required.",
    "permission_denied": "Permission denied.",
    "workspace_read_only": "The workspace is read-only.",
    "workspace_suspended": "The workspace is suspended.",
    "not_found": "Not found.",
    "method_not_allowed": "Method not allowed.",
    "unsupported_media_type": "Unsupported media type.",
    "rate_limited": "Too many requests.",
}

# DRF exception class -> problem code. Checked in order, so subclasses first.
_DRF_CODES = [
    (exceptions.ValidationError, "validation_error"),
    (exceptions.ParseError, "validation_error"),
    (exceptions.NotAuthenticated, "not_authenticated"),
    (exceptions.AuthenticationFailed, "not_authenticated"),
    (exceptions.PermissionDenied, "permission_denied"),
    (exceptions.NotFound, "not_found"),
    (exceptions.MethodNotAllowed, "method_not_allowed"),
    (exceptions.UnsupportedMediaType, "unsupported_media_type"),
    (exceptions.Throttled, "rate_limited"),
]


def problem(status_code: int, code: str, detail: str, **extra) -> dict:
    return {
        "type": PROBLEM_TYPE_BASE + code.replace("_", "-"),
        "title": TITLES.get(code, "Error."),
        "status": status_code,
        "code": code,
        "detail": detail,
        **extra,
    }


def problem_response(status_code: int, code: str, detail: str) -> JsonResponse:
    """For code that runs outside DRF, such as middleware."""
    return JsonResponse(problem(status_code, code, detail), status=status_code, content_type=PROBLEM_CONTENT_TYPE)


def problem_exception_handler(exc, context):
    if isinstance(exc, DomainError):
        return Response(problem(exc.status, exc.code, exc.detail), status=exc.status, content_type=PROBLEM_CONTENT_TYPE)
    if isinstance(exc, DjangoValidationError):
        exc = exceptions.ValidationError(exc.message_dict if hasattr(exc, "error_dict") else exc.messages)

    response = exception_handler(exc, context)
    if response is None:
        return None  # unexpected: Django's 500 handling takes over, with no details shown

    code = next((code for cls, code in _DRF_CODES if isinstance(exc, cls)), "error")
    extra = {}
    if code == "validation_error" and isinstance(exc, exceptions.ValidationError):
        errors = response.data if isinstance(response.data, dict) else {"non_field_errors": response.data}
        detail, extra = "One or more fields are invalid.", {"errors": errors}
    else:
        detail = str(getattr(exc, "detail", "")) or TITLES.get(code, "Error.")
    if code == "not_authenticated":
        # Session auth has no WWW-Authenticate challenge, so DRF would answer
        # 403; the contract distinguishes "not signed in" (401).
        response.status_code = status.HTTP_401_UNAUTHORIZED

    response.data = problem(response.status_code, code, detail, **extra)
    response.content_type = PROBLEM_CONTENT_TYPE
    return response
