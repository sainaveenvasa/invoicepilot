"""Customer write use cases (system design §3.1). The only code that writes
``customers_customer``; HTML views, the API and future imports all come here."""

from django.db import IntegrityError, transaction
from django.db.models import RestrictedError
from django.utils import timezone

from apps.accounts.models import normalize_email
from apps.billing.limits import check_plan_limit
from apps.core import errors
from apps.organizations import permissions

from . import selectors
from .models import Customer

UPDATABLE_FIELDS = ("name", "email", "company_name", "notes")
EMAIL_UNIQUE_CONSTRAINT = "customers_customer_email_uniq"
DUPLICATE_EMAIL = "A customer with this email already exists."


def _require_manage(ctx) -> None:
    permissions.require(ctx, "customers.manage")
    permissions.require_write_access(ctx)


def _clean(customer: Customer) -> None:
    customer.name = customer.name.strip()
    customer.company_name = customer.company_name.strip()
    customer.email = normalize_email(customer.email)
    # The (organization, email) constraint is checked below instead, so a
    # clash is a Duplicate (409 / field error) rather than a generic 400.
    customer.full_clean(validate_constraints=False)


def _ensure_email_free(ctx, customer: Customer) -> None:
    clash = selectors.scoped(ctx).filter(email=customer.email).exclude(pk=customer.pk).values_list("pk", flat=True)
    existing_id = clash.first()
    if existing_id is not None:
        # Same workspace, so revealing the ID is safe (API contracts §1.6).
        raise errors.Duplicate(DUPLICATE_EMAIL, field="email", existing_id=str(existing_id))


def _save(customer: Customer, **kwargs) -> None:
    """Save, turning a lost race on the email constraint into Duplicate."""
    try:
        with transaction.atomic():
            customer.save(**kwargs)
    except IntegrityError as error:
        if EMAIL_UNIQUE_CONSTRAINT in str(error):
            raise errors.Duplicate(DUPLICATE_EMAIL, field="email") from error
        raise


def create_customer(*, ctx, name: str, email: str, company_name: str = "", notes: str = "") -> Customer:
    """Every customer row counts toward the plan limit, deactivated ones
    included, so deactivating and reactivating can't get around it."""
    _require_manage(ctx)
    customer = Customer(organization=ctx.organization, name=name, email=email, company_name=company_name, notes=notes)
    _clean(customer)
    with transaction.atomic():
        check_plan_limit(ctx.organization, "customers", count=selectors.scoped(ctx).count)
        _ensure_email_free(ctx, customer)
        _save(customer)
    return customer


def update_customer(*, ctx, customer_id, version: int | None, **changes) -> Customer:
    """Compare-and-set on ``version`` (database design §11.6): ``version`` is
    the one the caller loaded, and None never matches. Raises VersionConflict
    with the current row when someone else saved first."""
    _require_manage(ctx)
    unknown = set(changes) - set(UPDATABLE_FIELDS)
    if unknown:
        raise ValueError(f"Not updatable: {sorted(unknown)}")

    with transaction.atomic():
        customer = selectors.get_customer(ctx=ctx, customer_id=customer_id, for_update=True)
        if customer.version != version:
            raise errors.VersionConflict(current=customer)
        if not changes:
            return customer
        for field, value in changes.items():
            setattr(customer, field, value)
        _clean(customer)
        if "email" in changes:
            _ensure_email_free(ctx, customer)
        customer.version += 1
        _save(customer, update_fields=[*changes, "version", "updated_at"])
    return customer


def _set_active(ctx, customer_id, active: bool) -> Customer:
    """Idempotent by state: repeating a transition returns the customer unchanged."""
    _require_manage(ctx)
    with transaction.atomic():
        customer = selectors.get_customer(ctx=ctx, customer_id=customer_id, for_update=True)
        if customer.is_active != active:
            customer.is_active = active
            customer.deactivated_at = None if active else timezone.now()
            customer.version += 1
            customer.save(update_fields=["is_active", "deactivated_at", "version", "updated_at"])
    return customer


def deactivate_customer(*, ctx, customer_id) -> Customer:
    """Hides the customer from new work; reminders due for them are skipped (FR-004)."""
    return _set_active(ctx, customer_id, active=False)


def reactivate_customer(*, ctx, customer_id) -> Customer:
    return _set_active(ctx, customer_id, active=True)


def delete_customer(*, ctx, customer_id) -> None:
    """Only a customer with no invoices can be deleted; otherwise InUse
    (Invoice.customer is RESTRICT). The row lock conflicts with the key-share
    lock a concurrent invoice insert takes, so the two serialize."""
    _require_manage(ctx)
    with transaction.atomic():
        customer = selectors.get_customer(ctx=ctx, customer_id=customer_id, for_update=True)
        try:
            customer.delete()
        except RestrictedError as error:
            raise errors.InUse("This customer has invoices, so it can't be deleted. Deactivate it instead.") from error
