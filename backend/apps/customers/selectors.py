"""Tenant-scoped customer reads. Every query starts from the acting user's
workspace, so another workspace's ID is simply not found (404, never 403)."""

import uuid

from django.db.models import Q

from apps.core import errors
from apps.organizations import permissions

from .models import Customer

ORDERINGS = ("name", "-created_at")


def _as_uuid(value) -> uuid.UUID | None:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def scoped(ctx):
    """Every customer in the workspace, for callers that have already checked
    ``customers.view`` or ``customers.manage``."""
    return Customer.objects.for_organization(ctx.organization)


def list_customers(*, ctx, q: str = "", is_active: bool | None = None, ordering: str = "name"):
    """API contracts §4: ``q`` matches the start of the name or email, ignoring case."""
    permissions.require(ctx, "customers.view")
    if ordering not in ORDERINGS:
        raise ValueError(f"Unsupported ordering {ordering!r}")
    customers = scoped(ctx)
    q = q.strip()
    if q:
        customers = customers.filter(Q(name__istartswith=q) | Q(email__istartswith=q))
    if is_active is not None:
        customers = customers.filter(is_active=is_active)
    # id breaks ties, so equal names keep a stable order across pages.
    return customers.order_by(ordering, "id")


def get_customer(*, ctx, customer_id, for_update: bool = False) -> Customer:
    """``for_update`` locks the row until the caller's transaction ends."""
    permissions.require(ctx, "customers.view")
    customer_id = _as_uuid(customer_id)
    customers = scoped(ctx).select_for_update() if for_update else scoped(ctx)
    customer = customers.filter(pk=customer_id).first() if customer_id else None
    if customer is None:
        raise errors.NotFound()
    return customer
