"""BR-010 / BR-017 and release criterion 2: cross-tenant access."""

import pytest
from django.apps import apps
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from invoicepilot.customers.models import Customer
from invoicepilot.invoices.models import Invoice, PaymentRecord
from invoicepilot.reminders.models import ReminderDelivery

pytestmark = pytest.mark.django_db


def tenant_models():
    return {
        model
        for model in apps.get_models()
        if any(f.name == "organization" and f.many_to_one and not f.null for f in model._meta.concrete_fields)
    }


def test_every_fk_between_tenant_tables_has_a_composite_tenant_constraint():
    """Guards future migrations: a new tenant->tenant FK without AddTenantForeignKey fails here."""
    tenant = tenant_models()
    expected = {
        (model._meta.db_table, field.related_model._meta.db_table, ("organization_id", field.column))
        for model in tenant
        for field in model._meta.concrete_fields
        if field.many_to_one and field.name != "organization" and field.related_model in tenant
    }
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT con.conrelid::regclass::text, con.confrelid::regclass::text,
                   ARRAY(SELECT a.attname
                         FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
                         JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
                         ORDER BY k.ord)
            FROM pg_constraint con
            WHERE con.contype = 'f'
            """
        )
        actual = {(table, ref, tuple(cols)) for table, ref, cols in cursor.fetchall()}

    assert len(expected) == 10
    assert expected - actual == set()


def test_invoice_cannot_reference_a_customer_from_another_workspace(
    org, other_org, make_customer, make_invoice, immediate_constraints
):
    foreign_customer = make_customer(other_org)
    with pytest.raises(IntegrityError, match="invoices_invoice_customer_tenant_fk"), transaction.atomic():
        make_invoice(org, customer=foreign_customer)


def test_cross_tenant_link_is_rejected_even_via_raw_update(
    org, other_org, make_customer, make_invoice, immediate_constraints
):
    invoice = make_invoice(org)
    foreign_customer = make_customer(other_org)
    with pytest.raises(IntegrityError), transaction.atomic():
        Invoice.objects.filter(pk=invoice.pk).update(customer=foreign_customer)


def test_payment_cannot_target_another_workspaces_invoice(org, other_org, make_invoice, immediate_constraints):
    foreign_invoice = make_invoice(other_org)
    with pytest.raises(IntegrityError, match="invoices_paymentrecord_invoice_tenant_fk"), transaction.atomic():
        PaymentRecord.objects.create(
            organization=org, invoice=foreign_invoice, amount=10, received_on=timezone.localdate()
        )


def test_reminder_delivery_cannot_mix_workspaces(org, other_org, make_invoice, make_step, immediate_constraints):
    invoice = make_invoice(org)
    foreign_step = make_step(other_org)
    with pytest.raises(IntegrityError, match="reminders_reminderdelivery_step_tenant_fk"), transaction.atomic():
        ReminderDelivery.objects.create(
            organization=org,
            invoice=invoice,
            step=foreign_step,
            invoice_due_date=invoice.due_date,
            idempotency_key="k",
            scheduled_for=timezone.now(),
        )


def test_for_organization_returns_only_that_workspaces_rows(org, other_org, make_customer):
    mine = make_customer(org, "a@example.com")
    make_customer(other_org, "a@example.com")
    assert list(Customer.objects.for_organization(org)) == [mine]
