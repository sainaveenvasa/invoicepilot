"""Invoice write use cases (system design §3.1). The only code that writes
``invoices_invoice``; the API, future HTML views and imports all come here.

Amounts arrive as ``Decimal`` in the invoice's currency and are stored as
integer minor units (core.money). Balance and status are never taken from
the caller: they are derived from the total and the payment records.
"""

import zoneinfo
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.billing.limits import check_plan_limit
from apps.core import errors, money
from apps.core.validators import validate_currency
from apps.customers import selectors as customer_selectors
from apps.organizations import permissions

from . import selectors
from .models import Invoice

UPDATABLE_FIELDS = ("invoice_number", "total_amount", "issue_date", "due_date", "description")
NUMBER_UNIQUE_CONSTRAINT = "invoices_invoice_number_uniq"
DUPLICATE_NUMBER = "An invoice with this number already exists."


def _require_manage(ctx) -> None:
    permissions.require(ctx, "invoices.manage")
    permissions.require_write_access(ctx)


def _field_error(field: str, check, *args):
    """Run a validator, reporting its error against ``field``."""
    try:
        return check(*args)
    except ValidationError as error:
        raise ValidationError({field: error.messages}) from error


def _clean(invoice: Invoice) -> None:
    invoice.invoice_number = invoice.invoice_number.strip()
    # The case-insensitive number constraint is checked below instead, so a
    # clash is a Duplicate (409) rather than a generic 400.
    invoice.full_clean(validate_constraints=False)


def _ensure_number_free(ctx, invoice: Invoice) -> None:
    clash = selectors.scoped(ctx).filter(invoice_number__iexact=invoice.invoice_number).exclude(pk=invoice.pk)
    existing_id = clash.values_list("pk", flat=True).first()
    if existing_id is not None:
        # Same workspace, so revealing the ID is safe (API contracts §1.5).
        raise errors.Duplicate(DUPLICATE_NUMBER, field="invoice_number", existing_id=str(existing_id))


def _save(invoice: Invoice, **kwargs) -> None:
    """Save, turning a lost race on the number constraint into Duplicate."""
    try:
        with transaction.atomic():
            invoice.save(**kwargs)
    except IntegrityError as error:
        if NUMBER_UNIQUE_CONSTRAINT in str(error):
            raise errors.Duplicate(DUPLICATE_NUMBER, field="invoice_number") from error
        raise


def _created_this_month(ctx) -> int:
    """Invoices created since the 1st of this month in the workspace's timezone (BR-012)."""
    local_now = timezone.now().astimezone(zoneinfo.ZoneInfo(ctx.organization.timezone))
    month_start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return selectors.scoped(ctx).filter(created_at__gte=month_start).count()


def create_invoice(
    *,
    ctx,
    customer_id,
    invoice_number: str,
    total_amount: Decimal,
    issue_date: date,
    due_date: date,
    currency: str | None = None,
    description: str = "",
    source: str = Invoice.Source.MANUAL,
) -> Invoice:
    """A new invoice is open, owes its full total, and counts toward the
    monthly plan limit. The customer must be an active customer of this
    workspace; anything else is NotFound, so a foreign ID reveals nothing."""
    _require_manage(ctx)
    customer = customer_selectors.get_customer(ctx=ctx, customer_id=customer_id)
    if not customer.is_active:
        raise errors.NotFound("No active customer with this ID.")
    currency = currency or ctx.organization.default_currency
    _field_error("currency", validate_currency, currency)
    total = _field_error("total_amount", money.to_minor, total_amount, currency)

    invoice = Invoice(
        organization=ctx.organization,
        customer=customer,
        invoice_number=invoice_number,
        currency=currency,
        total_amount_minor=total,
        balance_due_minor=total,
        issue_date=issue_date,
        due_date=due_date,
        description=description,
        source=source,
        created_by=ctx.user,
    )
    _clean(invoice)
    with transaction.atomic():
        check_plan_limit(ctx.organization, "invoices_per_month", count=lambda: _created_this_month(ctx))
        _ensure_number_free(ctx, invoice)
        _save(invoice)
    return invoice


def update_invoice(*, ctx, invoice_id, version: int | None, **changes) -> Invoice:
    """Compare-and-set on ``version`` (database design §11.6): ``version`` is
    the one the caller loaded, and None never matches.

    The customer and currency can't change. A void invoice can't be edited.
    A new total must cover what has already been paid; the balance and
    status are then re-derived from the payment records, under the row lock.
    """
    _require_manage(ctx)
    unknown = set(changes) - set(UPDATABLE_FIELDS)
    if unknown:
        raise ValueError(f"Not updatable: {sorted(unknown)}")

    with transaction.atomic():
        invoice = selectors.get_invoice(ctx=ctx, invoice_id=invoice_id, for_update=True)
        if invoice.version != version:
            raise errors.VersionConflict(current=invoice)
        if invoice.status == Invoice.Status.VOID:
            raise errors.InvalidState("A void invoice can't be edited.")
        if not changes:
            return invoice

        fields = [name for name in changes if name != "total_amount"]
        if "total_amount" in changes:
            total = _field_error("total_amount", money.to_minor, changes.pop("total_amount"), invoice.currency)
            paid = invoice.net_paid_minor()
            if total < paid:
                paid_display = money.format_minor(paid, invoice.currency)
                raise errors.InvalidState(
                    f"The total can't be less than the {paid_display} {invoice.currency} already paid."
                )
            invoice.total_amount_minor = total
            invoice.recalculate_balance()
            fields += ["total_amount_minor", "balance_due_minor", "status", "paid_at"]
        for name, value in changes.items():
            setattr(invoice, name, value)
        _clean(invoice)
        if "invoice_number" in changes:
            _ensure_number_free(ctx, invoice)
        invoice.version += 1
        _save(invoice, update_fields=[*fields, "version", "updated_at"])
    return invoice
