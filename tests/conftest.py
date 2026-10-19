from datetime import date
from decimal import Decimal

import pytest
from django.db import connection

from invoicepilot.customers.models import Customer
from invoicepilot.invoices.models import Invoice
from invoicepilot.organizations.models import Organization
from invoicepilot.reminders.models import EmailTemplate, ReminderPolicy, ReminderStep


@pytest.fixture
def immediate_constraints(db):
    """Composite tenant FKs are deferred to commit; check them per statement instead."""
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


@pytest.fixture
def make_org(db):
    def make(name="Workspace"):
        return Organization.objects.create(name=name, timezone="America/Denver", default_currency="USD")

    return make


@pytest.fixture
def org(make_org):
    return make_org("Workspace A")


@pytest.fixture
def other_org(make_org):
    return make_org("Workspace B")


@pytest.fixture
def make_customer(db):
    def make(organization, email="client@example.com"):
        return Customer.objects.create(organization=organization, name="Client", email=email)

    return make


@pytest.fixture
def make_invoice(make_customer):
    def make(organization, customer=None, invoice_number="INV-001", total="1000.00", **overrides):
        fields = {
            "currency": "USD",
            "total_amount": Decimal(total),
            "balance_due": Decimal(total),
            "issue_date": date(2026, 10, 1),
            "due_date": date(2026, 10, 15),
        }
        fields.update(overrides)
        return Invoice.objects.create(
            organization=organization,
            customer=customer or make_customer(organization),
            invoice_number=invoice_number,
            **fields,
        )

    return make


@pytest.fixture
def make_step(db):
    def make(organization, offset_days=3):
        policy = ReminderPolicy.objects.create(organization=organization)
        template = EmailTemplate.objects.create(
            organization=organization, name="Friendly", subject="Invoice {number}", body_text="..."
        )
        return ReminderStep.objects.create(
            organization=organization, policy=policy, position=1, offset_days=offset_days, template=template
        )

    return make
