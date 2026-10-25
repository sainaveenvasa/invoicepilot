from rest_framework.permissions import BasePermission

from apps.organizations import permissions


class IsWorkspaceMember(BasePermission):
    """The request has an active workspace the user belongs to. Raises (404 or
    403 workspace_suspended) instead of returning False, so DRF can't turn a
    missing workspace into a 403 that confirms it exists."""

    def has_permission(self, request, view):
        permissions.require_active_workspace(request.ctx)
        return True


class HasWorkspaceAction(BasePermission):
    """Checks ``view.required_actions[method]`` against the role's permission map."""

    def has_permission(self, request, view):
        action = view.required_actions.get(request.method)
        if action is None:
            return True  # the method isn't allowed anyway; DRF answers 405
        permissions.require(request.ctx, action)
        return True
