from django.urls import reverse
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core import errors
from apps.core.api.concurrency import etag, if_match_version
from apps.core.api.errors import PROBLEM_CONTENT_TYPE, problem
from apps.core.api.pagination import CursorPagination
from apps.customers import selectors, services
from apps.organizations import permissions
from apps.organizations.api.permissions import HasWorkspaceAction, IsWorkspaceMember

from .serializers import CustomerFilterSerializer, CustomerSerializer

PERMISSION_CLASSES = [IsAuthenticated, IsWorkspaceMember, HasWorkspaceAction]


def customer_response(customer, status_code=status.HTTP_200_OK, headers=None):
    headers = {"ETag": etag(customer.version), **(headers or {})}
    return Response(CustomerSerializer(customer).data, status=status_code, headers=headers)


class CustomerListView(APIView):
    permission_classes = PERMISSION_CLASSES
    required_actions = {"GET": "customers.view", "POST": "customers.manage"}

    def get(self, request):
        filters = CustomerFilterSerializer(data=request.query_params.dict())
        filters.is_valid(raise_exception=True)
        customers = selectors.list_customers(ctx=request.ctx, **filters.validated_data)
        paginator = CursorPagination()
        page = paginator.paginate_queryset(customers, request, view=self)
        return paginator.get_paginated_response(CustomerSerializer(page, many=True).data)

    def post(self, request):
        # Read-only workspaces are refused before validation, as on PATCH /workspace.
        permissions.require_write_access(request.ctx)
        serializer = CustomerSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        customer = services.create_customer(ctx=request.ctx, **serializer.validated_data)
        # A path, not an absolute URL: never built from the request's Host header.
        location = reverse("api:customer_detail", kwargs={"customer_id": customer.pk})
        return customer_response(customer, status.HTTP_201_CREATED, headers={"Location": location})


class CustomerDetailView(APIView):
    permission_classes = PERMISSION_CLASSES
    required_actions = {"GET": "customers.view", "PATCH": "customers.manage", "DELETE": "customers.manage"}

    def get(self, request, customer_id):
        return customer_response(selectors.get_customer(ctx=request.ctx, customer_id=customer_id))

    def patch(self, request, customer_id):
        # 404 for another workspace's ID comes first, whatever the headers or body.
        customer = selectors.get_customer(ctx=request.ctx, customer_id=customer_id)
        permissions.require_write_access(request.ctx)
        version = if_match_version(request)
        serializer = CustomerSerializer(customer, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        try:
            customer = services.update_customer(
                ctx=request.ctx, customer_id=customer.pk, version=version, **serializer.validated_data
            )
        except errors.VersionConflict as conflict:
            current = conflict.current
            body = problem(conflict.status, conflict.code, conflict.detail, current=CustomerSerializer(current).data)
            return Response(
                body, status=conflict.status, content_type=PROBLEM_CONTENT_TYPE, headers={"ETag": etag(current.version)}
            )
        return customer_response(customer)

    def delete(self, request, customer_id):
        services.delete_customer(ctx=request.ctx, customer_id=customer_id)
        return Response(status=status.HTTP_204_NO_CONTENT)


class CustomerDeactivateView(APIView):
    permission_classes = PERMISSION_CLASSES
    required_actions = {"POST": "customers.manage"}

    def post(self, request, customer_id):
        return customer_response(services.deactivate_customer(ctx=request.ctx, customer_id=customer_id))


class CustomerReactivateView(APIView):
    permission_classes = PERMISSION_CLASSES
    required_actions = {"POST": "customers.manage"}

    def post(self, request, customer_id):
        return customer_response(services.reactivate_customer(ctx=request.ctx, customer_id=customer_id))
