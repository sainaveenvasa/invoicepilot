from datetime import date
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.db import connection
from django.test import Client
from django.utils import timezone

from apps.accounts.models import User
from apps.core.context import RequestContext
from apps.customers.models import Customer
from apps.invoices.models import Invoice
from apps.organizations import services as organization_services
from apps.organizations.models import Organization
from apps.reminders.models import EmailTemplate, ReminderPolicy, ReminderStep

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture(autouse=True)
def _clear_cache():
    """Throttle counters live in the cache; never let them leak between tests."""
    cache.clear()
    yield
    cache.clear()


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


@pytest.fixture
def make_user(db):
    def make(email="dana@example.com", password=PASSWORD, verified=True, **extra):
        if verified:
            extra.setdefault("email_verified_at", timezone.now())
        return User.objects.create_user(email=email, password=password, **extra)

    return make


@pytest.fixture
def make_owner(make_user):
    """A user and the workspace they own, created through the real service."""

    def make(email, workspace_name="Workspace", *, writable=False):
        user = make_user(email=email)
        organization = organization_services.create_workspace(ctx=RequestContext.for_user(user), name=workspace_name)
        if writable:  # what billing.access.sync_workspace_access() does once a subscription is active
            Organization.objects.filter(pk=organization.pk).update(read_only_since=None)
            organization.refresh_from_db()
        return user, organization

    return make


@pytest.fixture
def client_for():
    """A test client signed in as the given user."""

    def make(user, *, enforce_csrf_checks=False):
        client = Client(enforce_csrf_checks=enforce_csrf_checks)
        client.force_login(user)
        return client

    return make
