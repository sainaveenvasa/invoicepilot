"""HTMX request helpers (system design §5.2)."""


def is_htmx(request) -> bool:
    """True for an HTMX request that expects a fragment.

    A history-restore request (the back button with historyCacheSize = 0)
    swaps the whole page, so it gets the full page like a normal request.
    """
    headers = request.headers
    return headers.get("HX-Request") == "true" and headers.get("HX-History-Restore-Request") != "true"
