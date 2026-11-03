from django.urls import reverse
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core import errors
from apps.core.api.concurrency import etag, if_match_version, version_conflict_response
from apps.core.api.pagination import CursorPagination
from apps.invoices import selectors, services
from apps.invoices.models import Invoice
from apps.organizations import permissions
from apps.organizations.api.permissions import HasWorkspaceAction, IsWorkspaceMember

from .serializers import InvoiceCreateSerializer, InvoiceFilterSerializer, InvoiceSerializer, InvoiceUpdateSerializer

PERMISSION_CLASSES = [IsAuthenticated, IsWorkspaceMember, HasWorkspaceAction]


def serializer_context(request) -> dict:
    return {"today": selectors.workspace_today(request.ctx.organization)}


def invoice_response(request, invoice, status_code=status.HTTP_200_OK, headers=None):
    headers = {"ETag": etag(invoice.version), **(headers or {})}
    data = InvoiceSerializer(invoice, context=serializer_context(request)).data
    return Response(data, status=status_code, headers=headers)


class InvoiceListView(APIView):
    permission_classes = PERMISSION_CLASSES
    required_actions = {"GET": "invoices.view", "POST": "invoices.manage"}

    def get(self, request):
        params = request.query_params
        filters = InvoiceFilterSerializer(data={**params.dict(), "status": params.getlist("status")})
        filters.is_valid(raise_exception=True)
        criteria = dict(filters.validated_data)
        invoices = selectors.list_invoices(ctx=request.ctx, statuses=criteria.pop("status"), **criteria)
        paginator = CursorPagination()
        page = paginator.paginate_queryset(invoices, request, view=self)
        data = InvoiceSerializer(page, many=True, context=serializer_context(request)).data
        return paginator.get_paginated_response(data)

    def post(self, request):
        # Read-only workspaces are refused before validation, as on PATCH /workspace.
        permissions.require_write_access(request.ctx)
        serializer = InvoiceCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        invoice = services.create_invoice(ctx=request.ctx, source=Invoice.Source.API, **serializer.validated_data)
        # A path, not an absolute URL: never built from the request's Host header.
        location = reverse("api:invoice_detail", kwargs={"invoice_id": invoice.pk})
        return invoice_response(request, invoice, status.HTTP_201_CREATED, headers={"Location": location})


class InvoiceDetailView(APIView):
    permission_classes = PERMISSION_CLASSES
    required_actions = {"GET": "invoices.view", "PATCH": "invoices.manage"}

    def get(self, request, invoice_id):
        return invoice_response(request, selectors.get_invoice(ctx=request.ctx, invoice_id=invoice_id))

    def patch(self, request, invoice_id):
        # 404 for another workspace's ID comes first, whatever the headers or body.
        invoice = selectors.get_invoice(ctx=request.ctx, invoice_id=invoice_id)
        permissions.require_write_access(request.ctx)
        version = if_match_version(request)
        serializer = InvoiceUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        try:
            invoice = services.update_invoice(
                ctx=request.ctx, invoice_id=invoice.pk, version=version, **serializer.validated_data
            )
        except errors.VersionConflict as conflict:
            current = InvoiceSerializer(conflict.current, context=serializer_context(request)).data
            return version_conflict_response(conflict, current)
        return invoice_response(request, invoice)
