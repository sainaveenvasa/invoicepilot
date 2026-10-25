import logging

from django.db import DatabaseError, connection
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

logger = logging.getLogger(__name__)


@never_cache
@require_safe
def healthz(request):
    """Liveness: the process is up and serving requests. Touches nothing else,
    so a database outage doesn't make the platform restart healthy web processes."""
    return JsonResponse({"status": "ok"})


@never_cache
@require_safe
def readyz(request):
    """Readiness: the process can reach PostgreSQL and should receive traffic."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except DatabaseError:
        logger.exception("Readiness check failed: database unavailable")
        return JsonResponse({"status": "unavailable", "database": "error"}, status=503)
    return JsonResponse({"status": "ok", "database": "ok"})
