"""Tenant-scoped invoice reads. Every query starts from the acting user's
workspace, so another workspace's ID is simply not found (404, never 403)."""

import zoneinfo
from datetime import date

from django.db.models import Q
from django.utils import timezone

from apps.core import errors
from apps.core.ids import parse_uuid
from apps.organizations import permissions

from .models import Invoice

# API ordering name → order_by. id breaks ties, so pages stay stable.
ORDERINGS = {
    "-issue_date": ("-issue_date", "-id"),
    "due_date": ("due_date", "id"),
    "-due_date": ("-due_date", "-id"),
    "balance_due": ("balance_due_minor", "id"),
}


def workspace_today(organization) -> date:
    """Overdue is judged by the workspace's calendar, not the server's (BR-004)."""
    return timezone.localdate(timezone=zoneinfo.ZoneInfo(organization.timezone))


def scoped(ctx):
    """Every invoice in the workspace, for callers that have already checked permissions."""
    return Invoice.objects.for_organization(ctx.organization)


def _overdue(today: date) -> Q:
    return Q(balance_due_minor__gt=0, due_date__lt=today) & ~Q(status=Invoice.Status.VOID)


def list_invoices(
    *,
    ctx,
    statuses=(),
    customer_id=None,
    overdue: bool | None = None,
    due_from: date | None = None,
    due_to: date | None = None,
    currency: str | None = None,
    q: str = "",
    ordering: str = "-issue_date",
):
    """API contracts §5. ``q`` matches the start of the invoice number, ignoring case."""
    permissions.require(ctx, "invoices.view")
    if ordering not in ORDERINGS:
        raise ValueError(f"Unsupported ordering {ordering!r}")
    invoices = scoped(ctx).select_related("customer")
    if statuses:
        invoices = invoices.filter(status__in=statuses)
    if customer_id is not None:
        invoices = invoices.filter(customer_id=customer_id)
    if overdue is not None:
        condition = _overdue(workspace_today(ctx.organization))
        invoices = invoices.filter(condition) if overdue else invoices.exclude(condition)
    if due_from is not None:
        invoices = invoices.filter(due_date__gte=due_from)
    if due_to is not None:
        invoices = invoices.filter(due_date__lte=due_to)
    if currency:
        invoices = invoices.filter(currency=currency)
    q = q.strip()
    if q:
        invoices = invoices.filter(invoice_number__istartswith=q)
    return invoices.order_by(*ORDERINGS[ordering])


def get_invoice(*, ctx, invoice_id, for_update: bool = False) -> Invoice:
    """``for_update`` takes FOR NO KEY UPDATE on the invoice only (database
    design §11.2): payments serialize on it, but inserts that reference it
    (ledger entries, deliveries) aren't blocked, and the customer isn't locked."""
    permissions.require(ctx, "invoices.view")
    invoice_id = parse_uuid(invoice_id)
    invoices = scoped(ctx).select_related("customer")
    if for_update:
        invoices = invoices.select_for_update(no_key=True, of=("self",))
    invoice = invoices.filter(pk=invoice_id).first() if invoice_id else None
    if invoice is None:
        raise errors.NotFound()
    return invoice
