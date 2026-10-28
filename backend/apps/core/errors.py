"""Domain errors raised by services and permission checks.

HTML views and the API map these to responses (system design §4); each error
carries its HTTP status and the problem ``code`` from API contracts §1.6.
"""


class DomainError(Exception):
    status = 400
    code = "error"
    default_detail = "The request could not be completed."

    def __init__(self, detail: str | None = None, **extra):
        self.detail = detail or self.default_detail
        # JSON-safe members added to the problem document, e.g. existing_id.
        self.extra = extra
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


class PlanLimitReached(DomainError):
    """Raised with ``limit`` and ``current`` extras (API contracts §1.6)."""

    status = 403
    code = "plan_limit_reached"
    default_detail = "Your plan's limit has been reached. Upgrade to add more."


class Duplicate(DomainError):
    """A unique value is already used in this workspace. ``field`` names it for
    form errors; ``existing_id`` is passed only when the caller may see it."""

    status = 409
    code = "duplicate"
    default_detail = "This value is already used."

    def __init__(self, detail: str | None = None, *, field: str | None = None, **extra):
        self.field = field
        super().__init__(detail, **extra)


class InUse(DomainError):
    status = 409
    code = "in_use"
    default_detail = "This record is referenced by other records. Deactivate it instead."


class VersionConflict(DomainError):
    """Someone else saved first (database design §11.6). ``current`` is the
    record as it is now, for the API to return and the UI to reload."""

    status = 412
    code = "version_conflict"
    default_detail = "This record was changed by someone else. Reload to see the latest."

    def __init__(self, detail: str | None = None, *, current=None):
        self.current = current
        super().__init__(detail)


class PreconditionRequired(DomainError):
    status = 428
    code = "precondition_required"
    default_detail = 'Send If-Match: "<version>" with the version you loaded.'
