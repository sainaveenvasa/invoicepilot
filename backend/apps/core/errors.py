"""Domain errors raised by services and permission checks.

HTML views and the API map these to responses (system design §4); each error
carries its HTTP status and the problem ``code`` from API contracts §1.6.
"""


class DomainError(Exception):
    status = 400
    code = "error"
    default_detail = "The request could not be completed."

    def __init__(self, detail: str | None = None):
        self.detail = detail or self.default_detail
        super().__init__(self.detail)


class NotFound(DomainError):
    """Also raised for another workspace's IDs, so a response never confirms they exist."""

    status = 404
    code = "not_found"
    default_detail = "Not found."


class PermissionDenied(DomainError):
    status = 403
    code = "permission_denied"
    default_detail = "You do not have permission to perform this action."


class WorkspaceSuspended(DomainError):
    status = 403
    code = "workspace_suspended"
    default_detail = "This workspace is suspended."


class ReadOnlyWorkspace(DomainError):
    status = 403
    code = "workspace_read_only"
    default_detail = "This workspace is read-only until a subscription is active."
