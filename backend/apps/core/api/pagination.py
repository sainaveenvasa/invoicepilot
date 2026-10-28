from rest_framework import pagination


class CursorPagination(pagination.CursorPagination):
    """API contracts §1.3: cursors stay stable while rows are inserted; no total count."""

    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100
    ordering = "-created_at"

    def get_ordering(self, request, queryset, view):
        """Keep the order the selector chose; it validates the client's choice.
        The cursor keys on the first field and uses an offset for ties, so a
        non-unique first field (``name``) still pages without gaps."""
        return tuple(queryset.query.order_by) or super().get_ordering(request, queryset, view)
