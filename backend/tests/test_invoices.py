"""Invoice management (FR-005, API contracts §5), integer minor-unit money
(database design §7) and tenant isolation (threat model T1, T2).

Most tests set up two writable owners, A and B, and prove that A can never
read, change or link to B's invoices or customers through any route.
"""

import zoneinfo
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.urls import reverse
from django.utils import timezone

from apps.billing.models import Plan, Subscription
from apps.core import errors, money
from apps.core.context import RequestContext
from apps.customers import services as customer_services
from apps.customers.models import Customer
from apps.invoices import selectors, services
from apps.invoices.models import Invoice, PaymentRecord
from apps.organizations import selectors as organization_selectors
from apps.organizations.models import Organization

pytestmark = pytest.mark.django_db

INVOICES = reverse("api:invoice_list")


@pytest.fixture
def owner_a(make_owner):
    return make_owner("a@example.com", "Workspace A", writable=True)


@pytest.fixture
def owner_b(make_owner):
    return make_owner("b@example.com", "Workspace B", writable=True)


def ctx_for(user, organization):
    membership = organization_selectors.get_membership(user=user, organization_id=organization.pk)
    return RequestContext.for_user(user, membership=membership)


@pytest.fixture
def ctx_a(owner_a):
    return ctx_for(*owner_a)


@pytest.fixture
def ctx_b(owner_b):
    return ctx_for(*owner_b)


@pytest.fixture
def customer_a(ctx_a):
    return customer_services.create_customer(ctx=ctx_a, name="Acme Corp", email="ap@acme.example")


@pytest.fixture
def foreign_customer(ctx_b):
    return customer_services.create_customer(ctx=ctx_b, name="Secret Client", email="secret@b.example")


@pytest.fixture
def foreign_invoice(ctx_b, foreign_customer):
    return create(ctx_b, foreign_customer, invoice_number="B-SECRET-1")


def create(ctx, customer, invoice_number="INV-1001", total_amount="1000.00", **fields):
    fields.setdefault("issue_date", date(2026, 10, 1))
    fields.setdefault("due_date", date(2026, 10, 31))
    return services.create_invoice(
        ctx=ctx, customer_id=customer.pk, invoice_number=invoice_number, total_amount=Decimal(total_amount), **fields
    )


def record_payment(invoice, amount_minor, kind="payment"):
    """Stands in for the ledger service (a later step): insert, then re-derive under a lock."""
    with transaction.atomic():
        locked = Invoice.objects.select_for_update().get(pk=invoice.pk)
        PaymentRecord.objects.create(
            organization_id=locked.organization_id,
            invoice=locked,
            kind=kind,
            amount_minor=amount_minor,
            received_on=date(2026, 10, 20),
        )
        locked.recalculate_balance()
        locked.save()
    return locked


def subscribe(organization, **limits):
    plan = Plan.objects.create(code="solo", name="Solo", stripe_price_id="price_solo", **limits)
    Subscription.objects.create(
        organization=organization,
        plan=plan,
        stripe_subscription_id="sub_1",
        status=Subscription.Status.ACTIVE,
        stripe_event_created=timezone.now(),
    )


def assert_problem(response, status, code):
    assert response.status_code == status, response.content
    assert response["Content-Type"] == "application/problem+json"
    assert response.json()["code"] == code


def snapshot(invoice):
    fresh = Invoice.objects.get(pk=invoice.pk)
    return (fresh.invoice_number, fresh.total_amount_minor, fresh.balance_due_minor, fresh.status, fresh.version)


# --- Money conversion ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "currency", "minor"),
    [("1250.50", "USD", 125050), ("0.01", "EUR", 1), ("120000", "JPY", 120000), ("7.5", "GBP", 750)],
)
def test_amounts_convert_to_minor_units_exactly(amount, currency, minor):
    assert money.to_minor(Decimal(amount), currency) == minor
    assert money.from_minor(minor, currency) == Decimal(amount)


@pytest.mark.parametrize(
    ("minor", "currency", "text"),
    [(125050, "USD", "1250.50"), (5, "USD", "0.05"), (120000, "JPY", "120000"), (money.MAX_MINOR, "USD", None)],
)
def test_minor_units_format_with_the_currencys_decimals(minor, currency, text):
    assert money.format_minor(minor, currency) == (text or "999999999999.99")


@pytest.mark.parametrize(
    ("amount", "currency", "message"),
    [
        ("10.005", "USD", "USD amounts allow at most 2 decimal places."),
        ("100.5", "JPY", "JPY amounts allow at most 0 decimal places."),
        ("0", "USD", "The amount must be greater than zero."),
        ("-1.00", "USD", "The amount must be greater than zero."),
        ("1000000000000.00", "USD", "The amount is too large."),
        ("NaN", "USD", "Enter a valid amount."),
    ],
)
def test_amounts_that_would_need_rounding_or_are_out_of_range_are_rejected(amount, currency, message):
    with pytest.raises(ValidationError) as caught:
        money.to_minor(Decimal(amount), currency)
    assert caught.value.messages == [message]


# --- Database constraints and composite tenant keys ---------------------------------


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"total": money.MAX_MINOR + 1}, "invoices_invoice_total_max"),
        ({"source": "fax"}, "invoices_invoice_source_valid"),
        ({"description": "x" * 5001}, "invoices_invoice_description_length"),
        ({"version": 0}, "invoices_invoice_version_positive"),
        ({"paid_at": timezone.now()}, "invoices_invoice_paid_at"),  # open, yet "paid at"
        ({"voided_at": timezone.now()}, "invoices_invoice_voided_at"),  # open, yet "voided at"
        ({"status": "unknown"}, "invoices_invoice_status_consistent"),
    ],
)
def test_database_rejects_inconsistent_invoices(org, make_invoice, overrides, constraint):
    with pytest.raises(IntegrityError, match=constraint), transaction.atomic():
        make_invoice(org, **overrides)


def test_raw_update_cannot_desynchronize_balance_and_status(org, make_invoice):
    invoice = make_invoice(org, total=10_000)
    for changes, constraint in [
        ({"balance_due_minor": 4_000}, "invoices_invoice_status_consistent"),  # partial, but still "open"
        ({"balance_due_minor": 10_001}, "invoices_invoice_balance_in_range"),
        ({"total_amount_minor": 0, "balance_due_minor": 0}, "invoices_invoice_total_positive"),
        ({"status": "paid"}, "invoices_invoice_status_consistent"),
    ]:
        with pytest.raises(IntegrityError, match=constraint), transaction.atomic():
            Invoice.objects.filter(pk=invoice.pk).update(**changes)


def test_payment_amounts_must_be_positive_minor_units(org, make_invoice):
    invoice = make_invoice(org)
    with pytest.raises(IntegrityError, match="invoices_payment_amount_positive"), transaction.atomic():
        PaymentRecord.objects.create(organization=org, invoice=invoice, amount_minor=0, received_on=date(2026, 10, 1))


def test_money_columns_are_bigint_minor_units():
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_name, column_name, data_type FROM information_schema.columns
            WHERE table_name IN ('invoices_invoice', 'invoices_paymentrecord')
              AND column_name IN ('total_amount_minor', 'balance_due_minor', 'amount_minor',
                                  'total_amount', 'balance_due', 'amount')
            """
        )
        columns = {(table, column): kind for table, column, kind in cursor.fetchall()}
    assert columns == {
        ("invoices_invoice", "total_amount_minor"): "bigint",
        ("invoices_invoice", "balance_due_minor"): "bigint",
        ("invoices_paymentrecord", "amount_minor"): "bigint",
    }


def test_invoice_customer_composite_fk_survives_the_migration():
    """FOREIGN KEY (organization_id, customer_id) → customers_customer (organization_id, id)."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT confrelid::regclass::text, condeferrable, condeferred,
                   ARRAY(SELECT a.attname FROM unnest(conkey) WITH ORDINALITY k(n, o)
                         JOIN pg_attribute a ON a.attrelid = conrelid AND a.attnum = k.n ORDER BY k.o)
            FROM pg_constraint WHERE conname = 'invoices_invoice_customer_tenant_fk'
            """
        )
        assert cursor.fetchone() == ("customers_customer", True, True, ["organization_id", "customer_id"])


@pytest.mark.django_db(transaction=True)
def test_minor_unit_migration_converts_existing_money_exactly_and_reverses():
    before, after = [("invoices", "0002_tenant_foreign_keys")], [("invoices", "0003_minor_units")]
    organization = Organization.objects.create(name="Legacy", default_currency="USD")
    customer = Customer.objects.create(organization=organization, name="Old Client", email="old@example.com")
    executor = MigrationExecutor(connection)
    executor.migrate(before)
    try:
        old = executor.loader.project_state(before).apps
        OldInvoice, OldPayment = old.get_model("invoices", "Invoice"), old.get_model("invoices", "PaymentRecord")
        common = {"organization_id": organization.pk, "customer_id": customer.pk, "issue_date": date(2026, 9, 1)}
        usd = OldInvoice.objects.create(
            invoice_number="USD-1",
            currency="USD",
            total_amount=Decimal("1250.50"),
            balance_due=Decimal("250.49"),
            status="partially_paid",
            due_date=date(2026, 9, 30),
            **common,
        )
        OldPayment.objects.create(
            organization_id=organization.pk, invoice=usd, amount=Decimal("1000.01"), received_on=date(2026, 9, 5)
        )
        jpy = OldInvoice.objects.create(
            invoice_number="JPY-1",
            currency="JPY",
            total_amount=Decimal("120000"),
            balance_due=Decimal("120000"),
            due_date=date(2026, 9, 30),
            **common,
        )

        executor = MigrationExecutor(connection)
        executor.migrate(after)
        new = executor.loader.project_state(after).apps
        Invoice_, Payment_ = new.get_model("invoices", "Invoice"), new.get_model("invoices", "PaymentRecord")
        assert Invoice_.objects.values_list("total_amount_minor", "balance_due_minor").get(pk=usd.pk) == (
            125050,
            25049,
        )
        assert Payment_.objects.get(invoice_id=usd.pk).amount_minor == 100001
        assert Invoice_.objects.values_list("total_amount_minor", "balance_due_minor").get(pk=jpy.pk) == (
            120000,
            120000,
        )
        assert Invoice_.objects.get(pk=usd.pk).version == 1

        executor = MigrationExecutor(connection)
        executor.migrate(before)
        old = executor.loader.project_state(before).apps
        restored = old.get_model("invoices", "Invoice").objects.get(pk=usd.pk)
        assert (restored.total_amount, restored.balance_due) == (Decimal("1250.50"), Decimal("250.49"))
        assert old.get_model("invoices", "PaymentRecord").objects.get().amount == Decimal("1000.01")
    finally:
        MigrationExecutor(connection).migrate(after)


@pytest.mark.django_db(transaction=True)
def test_minor_unit_migration_refuses_fractional_yen_instead_of_rounding():
    before, after = [("invoices", "0002_tenant_foreign_keys")], [("invoices", "0003_minor_units")]
    organization = Organization.objects.create(name="Legacy", default_currency="JPY")
    customer = Customer.objects.create(organization=organization, name="Old Client", email="old@example.com")
    executor = MigrationExecutor(connection)
    executor.migrate(before)
    try:
        OldInvoice = executor.loader.project_state(before).apps.get_model("invoices", "Invoice")
        bad = OldInvoice.objects.create(
            organization_id=organization.pk,
            customer_id=customer.pk,
            invoice_number="JPY-BAD",
            currency="JPY",
            total_amount=Decimal("100.50"),
            balance_due=Decimal("100.50"),
            issue_date=date(2026, 9, 1),
            due_date=date(2026, 9, 30),
        )
        with pytest.raises(Exception, match="Fractional amounts on zero-decimal invoices"):
            MigrationExecutor(connection).migrate(after)
        OldInvoice.objects.filter(pk=bad.pk).delete()
    finally:
        MigrationExecutor(connection).migrate(after)


# --- Services: create ----------------------------------------------------------------


def test_create_invoice_stores_minor_units_and_starts_open(ctx_a, owner_a, customer_a):
    invoice = create(ctx_a, customer_a, invoice_number="  INV-1001 ", total_amount="1250.50", description="Retainer")
    assert (invoice.total_amount_minor, invoice.balance_due_minor) == (125050, 125050)
    assert (invoice.status, invoice.currency, invoice.invoice_number) == ("open", "USD", "INV-1001")
    assert (invoice.organization, invoice.customer, invoice.created_by) == (owner_a[1], customer_a, owner_a[0])
    assert (invoice.version, invoice.source, invoice.reminders_paused) == (1, "manual", False)


def test_create_invoice_in_a_zero_decimal_currency(ctx_a, customer_a):
    invoice = create(ctx_a, customer_a, currency="JPY", total_amount="120000")
    assert invoice.total_amount_minor == 120000
    with pytest.raises(ValidationError) as caught:
        create(ctx_a, customer_a, invoice_number="INV-2", currency="JPY", total_amount="120000.50")
    assert caught.value.message_dict == {"total_amount": ["JPY amounts allow at most 0 decimal places."]}


@pytest.mark.parametrize(
    ("fields", "field"),
    [
        ({"total_amount": "0"}, "total_amount"),
        ({"total_amount": "10.001"}, "total_amount"),
        ({"currency": "KWD"}, "currency"),
        ({"invoice_number": "   "}, "invoice_number"),
        ({"invoice_number": "X" * 65}, "invoice_number"),
        ({"description": "x" * 5001}, "description"),
        ({"issue_date": date(2026, 10, 2), "due_date": date(2026, 10, 1)}, "due_date"),
    ],
)
def test_create_invoice_validates_its_fields(ctx_a, customer_a, fields, field):
    with pytest.raises(ValidationError) as caught:
        create(ctx_a, customer_a, **fields)
    assert field in caught.value.message_dict
    assert not Invoice.objects.exists()


def test_invoice_number_is_unique_per_workspace_ignoring_case(ctx_a, ctx_b, customer_a, foreign_customer):
    existing = create(ctx_a, customer_a, invoice_number="INV-1001")
    with pytest.raises(errors.Duplicate) as caught:
        create(ctx_a, customer_a, invoice_number="inv-1001")
    assert caught.value.extra == {"existing_id": str(existing.pk)}
    create(ctx_b, foreign_customer, invoice_number="INV-1001")  # another workspace is independent


def test_invoice_for_another_workspaces_customer_is_not_found(ctx_a, foreign_customer):
    with pytest.raises(errors.NotFound):
        create(ctx_a, foreign_customer)
    assert not Invoice.objects.exists()


def test_invoice_for_a_deactivated_customer_is_not_found(ctx_a, customer_a):
    customer_services.deactivate_customer(ctx=ctx_a, customer_id=customer_a.pk)
    with pytest.raises(errors.NotFound):
        create(ctx_a, customer_a)


def test_create_needs_a_writable_workspace(make_owner):
    user, organization = make_owner("ro@example.com", "Read-only")
    customer = Customer.objects.create(organization=organization, name="C", email="c@example.com")
    with pytest.raises(errors.ReadOnlyWorkspace):
        create(ctx_for(user, organization), customer)


def test_monthly_invoice_limit_counts_this_month_in_the_workspace_timezone(ctx_a, owner_a, customer_a):
    subscribe(owner_a[1], max_invoices_per_month=2)
    first = create(ctx_a, customer_a, invoice_number="INV-1")
    create(ctx_a, customer_a, invoice_number="INV-2")
    with pytest.raises(errors.PlanLimitReached) as caught:
        create(ctx_a, customer_a, invoice_number="INV-3")
    assert caught.value.extra == {"limit": 2, "current": 2}

    # An invoice from last month frees this month's allowance.
    local_month_start = timezone.now().astimezone(zoneinfo.ZoneInfo(owner_a[1].timezone)).replace(day=1, hour=0)
    Invoice.objects.filter(pk=first.pk).update(created_at=local_month_start - timedelta(hours=1))
    create(ctx_a, customer_a, invoice_number="INV-3")


# --- Services: update ----------------------------------------------------------------


def test_update_invoice_changes_fields_and_bumps_version(ctx_a, customer_a):
    invoice = create(ctx_a, customer_a)
    updated = services.update_invoice(
        ctx=ctx_a,
        invoice_id=invoice.pk,
        version=1,
        invoice_number=" INV-1001-A ",
        total_amount=Decimal("1500.00"),
        due_date=date(2026, 11, 15),
        description="Revised",
    )
    assert snapshot(updated) == ("INV-1001-A", 150000, 150000, "open", 2)
    assert (updated.due_date, updated.description) == (date(2026, 11, 15), "Revised")


@pytest.mark.parametrize("stale", [2, None], ids=["newer", "missing"])
def test_stale_version_update_is_rejected(ctx_a, customer_a, stale):
    invoice = create(ctx_a, customer_a)
    services.update_invoice(ctx=ctx_a, invoice_id=invoice.pk, version=1, description="First save")
    with pytest.raises(errors.VersionConflict) as caught:
        services.update_invoice(ctx=ctx_a, invoice_id=invoice.pk, version=1 if stale == 2 else None, description="x")
    assert (caught.value.current.description, caught.value.current.version) == ("First save", 2)


def test_total_cannot_drop_below_what_has_been_paid(ctx_a, customer_a):
    invoice = record_payment(create(ctx_a, customer_a, total_amount="1000.00"), 60000)
    with pytest.raises(errors.InvalidState, match=r"less than the 600\.00 USD already paid"):
        services.update_invoice(ctx=ctx_a, invoice_id=invoice.pk, version=1, total_amount=Decimal("599.99"))
    assert snapshot(invoice) == ("INV-1001", 100000, 40000, "partially_paid", 1)


@pytest.mark.parametrize(
    ("new_total", "balance", "status", "paid_at_set"),
    [
        ("600.00", 0, "paid", True),  # lowered to exactly what was paid
        ("800.00", 20000, "partially_paid", False),
        ("1200.00", 60000, "partially_paid", False),
    ],
)
def test_changing_the_total_rederives_balance_and_status_from_payments(
    ctx_a, customer_a, new_total, balance, status, paid_at_set
):
    invoice = record_payment(create(ctx_a, customer_a, total_amount="1000.00"), 60000)
    updated = services.update_invoice(ctx=ctx_a, invoice_id=invoice.pk, version=1, total_amount=Decimal(new_total))
    assert (updated.balance_due_minor, updated.status, updated.paid_at is not None) == (balance, status, paid_at_set)
    assert updated.amount_paid_minor == 60000


def test_raising_the_total_of_a_paid_invoice_reopens_it(ctx_a, customer_a):
    invoice = record_payment(create(ctx_a, customer_a, total_amount="100.00"), 10000)
    assert invoice.status == "paid"
    updated = services.update_invoice(ctx=ctx_a, invoice_id=invoice.pk, version=1, total_amount=Decimal("150.00"))
    assert (updated.balance_due_minor, updated.status, updated.paid_at) == (5000, "partially_paid", None)


def test_refunds_count_against_what_has_been_paid(ctx_a, customer_a):
    invoice = record_payment(create(ctx_a, customer_a, total_amount="1000.00"), 60000)
    invoice = record_payment(invoice, 10000, kind="refund")  # net paid: 500.00
    updated = services.update_invoice(ctx=ctx_a, invoice_id=invoice.pk, version=1, total_amount=Decimal("500.00"))
    assert (updated.balance_due_minor, updated.status) == (0, "paid")


def test_a_disputed_invoice_stays_disputed_after_an_edit(ctx_a, customer_a):
    invoice = create(ctx_a, customer_a)
    Invoice.objects.filter(pk=invoice.pk).update(status="disputed", disputed_at=timezone.now())
    updated = services.update_invoice(ctx=ctx_a, invoice_id=invoice.pk, version=1, total_amount=Decimal("900.00"))
    assert (updated.status, updated.balance_due_minor) == ("disputed", 90000)


def test_a_void_invoice_cannot_be_edited(ctx_a, customer_a):
    invoice = create(ctx_a, customer_a)
    Invoice.objects.filter(pk=invoice.pk).update(status="void", voided_at=timezone.now())
    with pytest.raises(errors.InvalidState, match="void invoice"):
        services.update_invoice(ctx=ctx_a, invoice_id=invoice.pk, version=1, description="Edited")
    assert Invoice.objects.get(pk=invoice.pk).description == ""


def test_moving_the_due_date_before_the_issue_date_is_invalid(ctx_a, customer_a):
    invoice = create(ctx_a, customer_a, issue_date=date(2026, 10, 1), due_date=date(2026, 10, 31))
    with pytest.raises(ValidationError) as caught:
        services.update_invoice(ctx=ctx_a, invoice_id=invoice.pk, version=1, due_date=date(2026, 9, 30))
    assert "due_date" in caught.value.message_dict
    with pytest.raises(ValidationError):
        services.update_invoice(ctx=ctx_a, invoice_id=invoice.pk, version=1, issue_date=date(2026, 11, 1))
    assert snapshot(invoice)[-1] == 1


def test_renumbering_onto_another_invoice_is_a_duplicate(ctx_a, customer_a):
    create(ctx_a, customer_a, invoice_number="INV-1")
    invoice = create(ctx_a, customer_a, invoice_number="INV-2")
    with pytest.raises(errors.Duplicate):
        services.update_invoice(ctx=ctx_a, invoice_id=invoice.pk, version=1, invoice_number="inv-1")
    services.update_invoice(ctx=ctx_a, invoice_id=invoice.pk, version=1, invoice_number="inv-2")  # its own number


@pytest.mark.parametrize("field", ["customer", "currency", "status", "balance_due_minor", "total_amount_minor"])
def test_update_refuses_immutable_and_derived_fields(ctx_a, customer_a, field):
    invoice = create(ctx_a, customer_a)
    with pytest.raises(ValueError, match="Not updatable"):
        services.update_invoice(ctx=ctx_a, invoice_id=invoice.pk, version=1, **{field: None})


@pytest.mark.parametrize(
    "operation",
    [
        lambda ctx, pk: services.update_invoice(ctx=ctx, invoice_id=pk, version=1, description="Pwned"),
        lambda ctx, pk: selectors.get_invoice(ctx=ctx, invoice_id=pk),
    ],
    ids=["update", "get"],
)
def test_services_cannot_reach_another_workspaces_invoice(ctx_a, foreign_invoice, operation):
    before = snapshot(foreign_invoice)
    with pytest.raises(errors.NotFound):
        operation(ctx_a, foreign_invoice.pk)
    assert snapshot(foreign_invoice) == before


# --- Selectors -------------------------------------------------------------------------


@pytest.fixture
def ledger(ctx_a, owner_a, customer_a):
    """Four invoices around "today" in the workspace's timezone."""
    today = selectors.workspace_today(owner_a[1])
    other = customer_services.create_customer(ctx=ctx_a, name="Beta LLC", email="ap@beta.example")

    def make(number, customer, total, issued, due, **fields):
        return create(
            ctx_a, customer, invoice_number=number, total_amount=total, issue_date=issued, due_date=due, **fields
        )

    invoices = {
        "overdue": make("INV-001", customer_a, "300.00", today - timedelta(days=40), today - timedelta(days=10)),
        "due_today": make("INV-002", customer_a, "100.00", today - timedelta(days=30), today),
        "future": make("INV-003", other, "200.00", today - timedelta(days=5), today + timedelta(days=25)),
        "yen": make("JP-001", other, "50000", today - timedelta(days=50), today - timedelta(days=20), currency="JPY"),
    }
    invoices["yen"] = record_payment(invoices["yen"], 50000)  # paid, so not overdue
    return today, invoices


def numbers(queryset):
    return [invoice.invoice_number for invoice in queryset]


def test_list_filters(ctx_a, customer_a, ledger):
    _, inv = ledger
    assert numbers(selectors.list_invoices(ctx=ctx_a)) == ["INV-003", "INV-002", "INV-001", "JP-001"]
    assert numbers(selectors.list_invoices(ctx=ctx_a, overdue=True)) == ["INV-001"]
    assert numbers(selectors.list_invoices(ctx=ctx_a, overdue=False)) == ["INV-003", "INV-002", "JP-001"]
    assert numbers(selectors.list_invoices(ctx=ctx_a, statuses=["paid"])) == ["JP-001"]
    assert numbers(selectors.list_invoices(ctx=ctx_a, customer_id=customer_a.pk)) == ["INV-002", "INV-001"]
    assert numbers(selectors.list_invoices(ctx=ctx_a, currency="JPY")) == ["JP-001"]
    assert numbers(selectors.list_invoices(ctx=ctx_a, q="inv-00")) == ["INV-003", "INV-002", "INV-001"]
    assert numbers(selectors.list_invoices(ctx=ctx_a, q="001")) == []  # prefix, not substring
    today = ledger[0]
    assert numbers(selectors.list_invoices(ctx=ctx_a, due_from=today, due_to=today)) == ["INV-002"]


def test_list_orderings(ctx_a, ledger):
    assert numbers(selectors.list_invoices(ctx=ctx_a, ordering="due_date")) == [
        "JP-001",
        "INV-001",
        "INV-002",
        "INV-003",
    ]
    assert numbers(selectors.list_invoices(ctx=ctx_a, ordering="balance_due")) == [
        "JP-001",
        "INV-002",
        "INV-003",
        "INV-001",
    ]


def test_overdue_follows_the_workspace_calendar(ctx_a, owner_a, customer_a):
    """At 23:30 UTC on the 15th it is already the 16th in Tokyo, so an invoice due on the 15th is overdue there."""
    organization = owner_a[1]
    instant = datetime(2026, 10, 15, 23, 30, tzinfo=zoneinfo.ZoneInfo("UTC"))
    invoice = create(ctx_a, customer_a, issue_date=date(2026, 10, 1), due_date=date(2026, 10, 15))
    for tz, overdue in [("UTC", False), ("Asia/Tokyo", True), ("America/Denver", False)]:
        Organization.objects.filter(pk=organization.pk).update(timezone=tz)
        ctx = ctx_for(*owner_a)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(timezone, "now", lambda: instant)
            assert (invoice in selectors.list_invoices(ctx=ctx, overdue=True)) is overdue, tz


def test_list_never_includes_another_workspaces_invoices(ctx_a, customer_a, foreign_invoice, foreign_customer):
    create(ctx_a, customer_a, invoice_number=foreign_invoice.invoice_number)
    assert foreign_invoice not in selectors.list_invoices(ctx=ctx_a)
    assert list(selectors.list_invoices(ctx=ctx_a, customer_id=foreign_customer.pk)) == []


# --- API -------------------------------------------------------------------------------


def detail_url(invoice):
    return reverse("api:invoice_detail", kwargs={"invoice_id": invoice.pk})


@pytest.fixture
def api_a(owner_a, client_for):
    return client_for(owner_a[0])


def body_for(customer, **fields):
    return {
        "customer_id": str(customer.pk),
        "invoice_number": "INV-1001",
        "total_amount": "1250.50",
        "issue_date": "2026-10-01",
        "due_date": "2026-10-31",
        **fields,
    }


def post_json(client, url, body, **headers):
    return client.post(url, body, content_type="application/json", **headers)


def patch_json(client, url, body, **headers):
    return client.patch(url, body, content_type="application/json", **headers)


def test_api_requires_authentication(client):
    assert_problem(client.get(INVOICES), 401, "not_authenticated")


def test_api_create_returns_the_invoice_with_money_as_decimal_strings(api_a, customer_a):
    response = post_json(api_a, INVOICES, body_for(customer_a, description="Retainer"))
    assert response.status_code == 201, response.content
    invoice = Invoice.objects.get()
    assert (invoice.total_amount_minor, invoice.source) == (125050, "api")
    assert response["Location"] == detail_url(invoice)
    assert response["ETag"] == '"1"'
    body = response.json()
    assert body["customer"] == {"id": str(customer_a.pk), "name": "Acme Corp", "email": "ap@acme.example"}
    assert (body["total_amount"], body["balance_due"], body["amount_paid"]) == ("1250.50", "1250.50", "0.00")
    assert (body["currency"], body["status"], body["source"], body["version"]) == ("USD", "open", "api", 1)
    assert (body["issue_date"], body["due_date"]) == ("2026-10-01", "2026-10-31")


def test_money_is_serialized_as_strings(api_a, ctx_a, customer_a):
    create(ctx_a, customer_a, invoice_number="JP-1", currency="JPY", total_amount="120000")
    for invoice in api_a.get(INVOICES).json()["results"]:
        for field in ("total_amount", "balance_due", "amount_paid"):
            assert isinstance(invoice[field], str), field
    assert api_a.get(INVOICES).json()["results"][0]["total_amount"] == "120000"


@pytest.mark.parametrize(
    ("fields", "field", "message"),
    [
        ({"total_amount": 1250.5}, "total_amount", 'Enter the amount as a decimal string, for example "1250.50".'),
        ({"total_amount": "1e3"}, "total_amount", 'Enter the amount as a decimal string, for example "1250.50".'),
        ({"total_amount": "12.345"}, "total_amount", "USD amounts allow at most 2 decimal places."),
        ({"total_amount": "0.00"}, "total_amount", "The amount must be greater than zero."),
        ({"total_amount": "-5.00"}, "total_amount", "The amount must be greater than zero."),
        ({"currency": "JPY", "total_amount": "10.50"}, "total_amount", "JPY amounts allow at most 0 decimal places."),
        ({"currency": "XYZ"}, "currency", '"XYZ" is not a valid choice.'),
        ({"due_date": "2026-09-30"}, "due_date", "The due date can't be before the issue date."),
        ({"balance_due": "0.00"}, "balance_due", "This field is read-only."),
        ({"status": "paid"}, "status", "This field is read-only."),
        ({"source": "manual"}, "source", "This field is read-only."),
        ({"version": 5}, "version", "This field is read-only."),
        ({"organization": "x"}, "organization", "Unknown field."),
        ({"total_amount_minor": 1}, "total_amount_minor", "Unknown field."),
    ],
)
def test_api_create_rejects_invalid_bodies(api_a, customer_a, fields, field, message):
    response = post_json(api_a, INVOICES, body_for(customer_a, **fields))
    assert_problem(response, 400, "validation_error")
    assert response.json()["errors"][field] == [message]
    assert not Invoice.objects.exists()


def test_api_create_for_another_workspaces_customer_is_404(api_a, foreign_customer):
    response = post_json(api_a, INVOICES, body_for(foreign_customer))
    assert_problem(response, 404, "not_found")
    assert "Secret" not in response.content.decode()
    assert not Invoice.objects.exists()


def test_api_create_duplicate_number_is_409(api_a, ctx_a, customer_a):
    existing = create(ctx_a, customer_a, invoice_number="INV-1001")
    response = post_json(api_a, INVOICES, body_for(customer_a, invoice_number="inv-1001"))
    assert_problem(response, 409, "duplicate")
    assert response.json()["existing_id"] == str(existing.pk)


def test_api_create_over_the_monthly_limit_is_403(api_a, ctx_a, owner_a, customer_a):
    subscribe(owner_a[1], max_invoices_per_month=1)
    create(ctx_a, customer_a, invoice_number="INV-1")
    response = post_json(api_a, INVOICES, body_for(customer_a, invoice_number="INV-2"))
    assert_problem(response, 403, "plan_limit_reached")
    assert (response.json()["limit"], response.json()["current"]) == (1, 1)


def test_api_writes_on_a_read_only_workspace_are_refused(make_owner, client_for, make_invoice):
    user, organization = make_owner("ro@example.com", "Read-only")
    invoice = make_invoice(organization)
    client = client_for(user)
    customer = invoice.customer
    assert_problem(post_json(client, INVOICES, body_for(customer, invoice_number="N")), 403, "workspace_read_only")
    response = patch_json(client, detail_url(invoice), {"description": "x"}, HTTP_IF_MATCH='"1"')
    assert_problem(response, 403, "workspace_read_only")
    assert client.get(INVOICES).json()["results"][0]["invoice_number"] == "INV-001"  # reads still work (D3)


def test_api_get_includes_overdue_and_etag(api_a, ctx_a, owner_a, customer_a):
    today = selectors.workspace_today(owner_a[1])
    invoice = create(ctx_a, customer_a, issue_date=today - timedelta(days=30), due_date=today - timedelta(days=19))
    invoice = record_payment(invoice, 60000)
    response = api_a.get(detail_url(invoice))
    body = response.json()
    assert response["ETag"] == '"1"'
    assert (body["is_overdue"], body["days_overdue"]) == (True, 19)
    assert (body["balance_due"], body["amount_paid"], body["status"]) == ("400.00", "600.00", "partially_paid")


def test_patch_requires_if_match(api_a, ctx_a, customer_a):
    invoice = create(ctx_a, customer_a)
    assert_problem(patch_json(api_a, detail_url(invoice), {"description": "x"}), 428, "precondition_required")


def test_patch_with_a_stale_version_is_412_with_current(api_a, ctx_a, customer_a):
    invoice = create(ctx_a, customer_a)
    services.update_invoice(ctx=ctx_a, invoice_id=invoice.pk, version=1, description="Saved first")
    response = patch_json(api_a, detail_url(invoice), {"description": "Lost"}, HTTP_IF_MATCH='"1"')
    assert_problem(response, 412, "version_conflict")
    assert (response.json()["current"]["description"], response["ETag"]) == ("Saved first", '"2"')
    assert response.json()["current"]["total_amount"] == "1000.00"


def test_patch_with_the_current_version_saves(api_a, ctx_a, customer_a):
    invoice = create(ctx_a, customer_a)
    response = patch_json(
        api_a, detail_url(invoice), {"total_amount": "1300.00", "due_date": "2026-11-30"}, HTTP_IF_MATCH='"1"'
    )
    assert response.status_code == 200, response.content
    body = response.json()
    assert (body["total_amount"], body["balance_due"], body["due_date"], body["version"]) == (
        "1300.00",
        "1300.00",
        "2026-11-30",
        2,
    )
    assert response["ETag"] == '"2"'


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"customer_id": "0192a8f1-0c4d-7e5f-8a9b-0c1d2e3f4a5b"}, "customer_id"),
        ({"currency": "EUR"}, "currency"),
        ({"balance_due": "0.00"}, "balance_due"),
        ({"status": "paid"}, "status"),
        ({"paid_at": "2026-10-01T00:00:00Z"}, "paid_at"),
        ({"reminders_paused": True}, "reminders_paused"),
    ],
)
def test_patch_refuses_immutable_and_derived_fields(api_a, ctx_a, customer_a, body, field):
    invoice = create(ctx_a, customer_a)
    response = patch_json(api_a, detail_url(invoice), body, HTTP_IF_MATCH='"1"')
    assert_problem(response, 400, "validation_error")
    assert response.json()["errors"][field] == ["This field is read-only."]
    assert snapshot(invoice) == ("INV-1001", 100000, 100000, "open", 1)


def test_patch_below_the_amount_paid_is_409_invalid_state(api_a, ctx_a, customer_a):
    invoice = record_payment(create(ctx_a, customer_a), 100000)
    response = patch_json(api_a, detail_url(invoice), {"total_amount": "999.99"}, HTTP_IF_MATCH='"1"')
    assert_problem(response, 409, "invalid_state")
    assert "1000.00 USD already paid" in response.json()["detail"]


def test_patch_on_a_void_invoice_is_409_invalid_state(api_a, ctx_a, customer_a):
    invoice = create(ctx_a, customer_a)
    Invoice.objects.filter(pk=invoice.pk).update(status="void", voided_at=timezone.now())
    response = patch_json(api_a, detail_url(invoice), {"description": "x"}, HTTP_IF_MATCH='"1"')
    assert_problem(response, 409, "invalid_state")


def test_invoices_cannot_be_deleted(api_a, ctx_a, customer_a):
    invoice = create(ctx_a, customer_a)
    assert_problem(api_a.delete(detail_url(invoice)), 405, "method_not_allowed")
    assert Invoice.objects.filter(pk=invoice.pk).exists()


def test_api_list_paginates_and_filters(api_a, ledger):
    first = api_a.get(INVOICES, {"page_size": 2}).json()
    assert set(first) == {"results", "next", "previous"}
    assert [i["invoice_number"] for i in first["results"]] == ["INV-003", "INV-002"]
    second = api_a.get(first["next"]).json()
    assert [i["invoice_number"] for i in second["results"]] == ["INV-001", "JP-001"]

    def numbers_for(params):
        return [i["invoice_number"] for i in api_a.get(INVOICES, params).json()["results"]]

    assert numbers_for({"overdue": "true"}) == ["INV-001"]
    assert numbers_for({"status": ["paid", "open"], "ordering": "due_date"}) == [
        "JP-001",
        "INV-001",
        "INV-002",
        "INV-003",
    ]
    assert numbers_for({"currency": "JPY"}) == ["JP-001"]


@pytest.mark.parametrize(
    "params",
    [
        {"status": "late"},
        {"ordering": "total"},
        {"customer_id": "nope"},
        {"due_from": "2026-10-10", "due_to": "2026-10-01"},
        {"overdue": "maybe"},
    ],
)
def test_api_list_rejects_invalid_filters(api_a, params):
    assert_problem(api_a.get(INVOICES, params), 400, "validation_error")


def test_unsafe_invoice_api_methods_require_a_csrf_token(owner_a, client_for, customer_a):
    client = client_for(owner_a[0], enforce_csrf_checks=True)
    assert_problem(post_json(client, INVOICES, body_for(customer_a)), 403, "permission_denied")
    assert not Invoice.objects.exists()


@pytest.mark.parametrize("invoice_id", ["not-a-uuid", "00000000-0000-0000-0000-000000000000"])
def test_api_malformed_and_unknown_ids_are_problem_404s(api_a, invoice_id):
    assert_problem(api_a.get(reverse("api:invoice_detail", kwargs={"invoice_id": invoice_id})), 404, "not_found")


@pytest.mark.parametrize(("method", "headers"), [("get", {}), ("patch", {"HTTP_IF_MATCH": '"1"'})])
def test_cross_workspace_ids_return_404(api_a, foreign_invoice, method, headers):
    """Threat model T2: every {id} route and method answers 404 for another
    workspace's invoice, reveals nothing about it, and changes nothing."""
    before = snapshot(foreign_invoice)
    body = {"total_amount": "1.00", "description": "Pwned"}
    response = getattr(api_a, method)(detail_url(foreign_invoice), body, content_type="application/json", **headers)
    assert_problem(response, 404, "not_found")
    assert "SECRET" not in response.content.decode()
    assert snapshot(foreign_invoice) == before


@pytest.mark.parametrize("target", ["list", "detail"])
def test_workspace_header_for_another_workspace_is_404(api_a, foreign_invoice, target):
    url = INVOICES if target == "list" else detail_url(foreign_invoice)
    response = api_a.get(url, HTTP_X_WORKSPACE_ID=str(foreign_invoice.organization_id))
    assert_problem(response, 404, "not_found")


def test_api_list_never_includes_another_workspaces_invoices(api_a, ctx_a, customer_a, foreign_invoice):
    create(ctx_a, customer_a)
    results = api_a.get(INVOICES).json()["results"]
    results += api_a.get(INVOICES, {"customer_id": str(foreign_invoice.customer_id)}).json()["results"]
    assert str(foreign_invoice.pk) not in {i["id"] for i in results}
