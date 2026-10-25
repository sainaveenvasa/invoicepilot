from apps.core.api.errors import problem_response
from apps.core.context import RequestContext

from . import selectors

SESSION_KEY = "active_organization_id"
WORKSPACE_HEADER = "X-Workspace-ID"


def set_active_workspace(request, organization) -> None:
    request.session[SESSION_KEY] = str(organization.pk)


class TenantMiddleware:
    """Resolve the active workspace and attach ``request.ctx`` (system design §5.1).

    The workspace always comes from one of the user's own memberships, looked
    up on every request, so a removed membership takes effect immediately and
    a workspace ID alone never grants access. Order of preference:

    1. API only: the ``X-Workspace-ID`` header. Not one of the user's
       workspaces → 404, whatever the endpoint.
    2. The workspace chosen earlier in this session. A stale choice (the
       membership is gone) is dropped silently.
    3. The user's only workspace, if they have exactly one.

    Otherwise ``ctx.membership`` is None, and views that need a workspace send
    the user to the picker (HTML) or answer 404 (API). Suspended workspaces
    are still resolved here so the picker and ``/me`` keep working; the
    permission checks reject them.
    """

    SCOPED_PREFIXES = ("/app/", "/api/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        is_api = request.path.startswith("/api/")
        source = "api" if is_api else "web"
        request.ctx = RequestContext.anonymous(source=source)

        if request.path.startswith(self.SCOPED_PREFIXES) and request.user.is_authenticated:
            header = request.headers.get(WORKSPACE_HEADER) if is_api else None
            if header is not None:
                membership = selectors.get_membership(user=request.user, organization_id=header)
                if membership is None:
                    return problem_response(404, "not_found", "Not found.")
            else:
                membership = self._from_session(request)
            request.ctx = RequestContext.for_user(request.user, membership=membership, source=source)

        return self.get_response(request)

    @staticmethod
    def _from_session(request):
        chosen = request.session.get(SESSION_KEY)
        if chosen:
            membership = selectors.get_membership(user=request.user, organization_id=chosen)
            if membership is not None:
                return membership
            del request.session[SESSION_KEY]
        return selectors.get_sole_membership(request.user)
