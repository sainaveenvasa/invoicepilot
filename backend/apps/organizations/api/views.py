from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.organizations import permissions, selectors, services

from .permissions import HasWorkspaceAction, IsWorkspaceMember
from .serializers import MembershipSummarySerializer, WorkspaceSerializer


class MeView(APIView):
    """The signed-in user and the workspaces they can open. Needs no active
    workspace, so a client can pick one from ``memberships``."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        user, ctx = request.user, request.ctx
        return Response(
            {
                "id": str(user.pk),
                "email": user.email,
                "full_name": user.full_name,
                "email_verified": user.is_email_verified,
                "memberships": MembershipSummarySerializer(selectors.memberships_for_user(user), many=True).data,
                "active_organization_id": str(ctx.organization.pk) if ctx.organization else None,
            }
        )


class WorkspaceView(APIView):
    permission_classes = [IsAuthenticated, IsWorkspaceMember, HasWorkspaceAction]
    required_actions = {"GET": "workspace.view", "PATCH": "workspace.update"}

    def get(self, request):
        return Response(WorkspaceSerializer(request.ctx.organization).data)

    def patch(self, request):
        # Read-only workspaces are refused before validation, so the answer
        # doesn't depend on whether the body happened to be valid.
        permissions.require_write_access(request.ctx)
        serializer = WorkspaceSerializer(request.ctx.organization, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        organization = services.update_workspace(ctx=request.ctx, **serializer.validated_data)
        return Response(WorkspaceSerializer(organization).data)
