"""FR-005, FR-006, BR-002..BR-004: invoice integrity and balance derivation."""

from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import RestrictedError

from invoicepilot.invoices.models import Invoice, PaymentRecord

pytestmark = pytest.mark.django_db


def pay(invoice, amount, kind="payment"):
    return PaymentRecord.objects.create(
        organization=invoice.organization, invoice=invoice, kind=kind, amount=Decimal(amount), received_on=date(2026, 10, 20)
    )


def test_invoice_number_unique_per_workspace_case_insensitive(org, other_org, make_customer, make_invoice):
    customer = make_customer(org)
    make_invoice(org, customer=customer, invoice_number="INV-001")
    with pytest.raises(IntegrityError, match="invoices_invoice_number_uniq"), transaction.atomic():
        make_invoice(org, customer=customer, invoice_number="inv-001")
    make_invoice(other_org, invoice_number="INV-001")  # other workspaces are independent


@pytest.mark.parametrize(
    "overrides, constraint",
    [
        ({"total": "0.00"}, "invoices_invoice_total_positive"),
        ({"balance_due": Decimal("1000.01")}, "invoices_invoice_balance_in_range"),
        ({"balance_due": Decimal("-1.00")}, "invoices_invoice_balance_in_range"),
        ({"due_date": date(2026, 9, 30)}, "invoices_invoice_due_after_issue"),
        ({"status": "paid"}, "invoices_invoice_status_consistent"),
        ({"balance_due": Decimal("400.00")}, "invoices_invoice_status_consistent"),  # partial balance but "open"
        ({"status": "disputed"}, "invoices_invoice_status_consistent"),  # disputed_at missing
        ({"reminders_paused": True}, "invoices_invoice_pause_consistent"),
    ],
)
def test_database_rejects_invalid_invoices(org, make_invoice, overrides, constraint):
    with pytest.raises(IntegrityError, match=constraint), transaction.atomic():
        make_invoice(org, **overrides)


def test_balance_and_status_follow_payment_records(org, make_invoice):
    invoice = make_invoice(org, total="1000.00")

    pay(invoice, "400.00")
    invoice.recalculate_balance()
    invoice.save()
    assert (invoice.balance_due, invoice.status) == (Decimal("600.00"), Invoice.Status.PARTIALLY_PAID)

    credit = pay(invoice, "600.00", kind="credit")
    invoice.recalculate_balance()
    invoice.save()
    assert (invoice.balance_due, invoice.status) == (Decimal("0.00"), Invoice.Status.PAID)
    assert invoice.paid_at is not None

    refund = pay(invoice, "100.00", kind="refund")
    invoice.recalculate_balance()
    invoice.save()
    assert (invoice.balance_due, invoice.status, invoice.paid_at) == (
        Decimal("100.00"),
        Invoice.Status.PARTIALLY_PAID,
        None,
    )

    for record in (credit, refund):
        record.voided_at = invoice.updated_at
        record.save()
    invoice.recalculate_balance()
    invoice.save()
    assert (invoice.balance_due, invoice.status) == (Decimal("600.00"), Invoice.Status.PARTIALLY_PAID)


def test_overpayment_is_refused(org, make_invoice):
    invoice = make_invoice(org, total="100.00")
    pay(invoice, "150.00")
    with pytest.raises(ValidationError):
        invoice.recalculate_balance()


def test_dispute_survives_partial_payment_and_paid_overrides_it(org, make_invoice):
    invoice = make_invoice(org, total="100.00")
    invoice.disputed_at = invoice.created_at
    invoice.recalculate_balance()
    invoice.save()
    assert invoice.status == Invoice.Status.DISPUTED

    pay(invoice, "100.00")
    invoice.recalculate_balance()
    invoice.save()
    assert invoice.status == Invoice.Status.PAID


def test_overdue_requires_past_due_date_and_open_balance(org, make_invoice):
    invoice = make_invoice(org, due_date=date(2026, 10, 15))
    assert not invoice.is_overdue(as_of=date(2026, 10, 15))
    assert invoice.is_overdue(as_of=date(2026, 10, 16))
    invoice.balance_due = Decimal("0")
    assert not invoice.is_overdue(as_of=date(2026, 10, 16))


def test_customer_with_invoices_cannot_be_deleted(org, make_invoice):
    invoice = make_invoice(org)
    with pytest.raises(RestrictedError):
        invoice.customer.delete()


def test_deleting_a_workspace_cascades_through_restricted_children(org, make_invoice):
    invoice = make_invoice(org)
    pay(invoice, "10.00")
    org.delete()
    assert not Invoice.objects.exists()
