"""Identity, reminders, suppression, billing, webhooks, imports and audit constraints."""

from datetime import time, timedelta

import pytest
from django.db import IntegrityError, ProgrammingError, connection, transaction
from django.utils import timezone

from invoicepilot.accounts.models import User
from invoicepilot.audit.models import AuditEvent
from invoicepilot.billing.models import Plan, Subscription
from invoicepilot.imports.models import ImportBatch, ImportRow
from invoicepilot.organizations.models import Membership
from invoicepilot.reminders.models import EmailSuppression, ReminderDelivery, ReminderPolicy
from invoicepilot.webhooks.models import WebhookEvent

pytestmark = pytest.mark.django_db


# --- Accounts (FR-001, FR-003) ---------------------------------------------


def test_user_email_is_unique_ignoring_case():
    user = User.objects.create_user("Jane@Example.com", "pw")
    assert user.email == "jane@example.com"
    with pytest.raises(IntegrityError), transaction.atomic():
        User.objects.create_user("JANE@example.COM", "pw")
    with pytest.raises(IntegrityError, match="accounts_user_email_lowercase"), transaction.atomic():
        User.objects.filter(pk=user.pk).update(email="Jane@Example.com")


def test_workspace_has_exactly_one_owner(org):
    Membership.objects.create(organization=org, user=User.objects.create_user("a@example.com"))
    with pytest.raises(IntegrityError, match="organizations_membership_one_owner"), transaction.atomic():
        Membership.objects.create(organization=org, user=User.objects.create_user("b@example.com"))


# --- Reminders (BR-008, FR-008) --------------------------------------------


def delivery(invoice, step, due_date=None, key=None, **fields):
    due_date = due_date or invoice.due_date
    return ReminderDelivery.objects.create(
        organization=invoice.organization,
        invoice=invoice,
        step=step,
        invoice_due_date=due_date,
        idempotency_key=key or ReminderDelivery.build_idempotency_key(invoice.id, step.id, due_date),
        scheduled_for=timezone.now(),
        **fields,
    )


def test_one_logical_reminder_per_invoice_step_and_due_date(org, make_invoice, make_step):
    invoice, step = make_invoice(org), make_step(org)
    delivery(invoice, step)
    # A scheduler re-run that builds a different key still cannot double-book.
    with pytest.raises(IntegrityError, match="reminders_delivery_logical_uniq"), transaction.atomic():
        delivery(invoice, step, key="different-key")
    # A moved due date is a new logical reminder (BR-009).
    delivery(invoice, step, due_date=invoice.due_date + timedelta(days=14))


@pytest.mark.parametrize(
    "fields, constraint",
    [
        ({"status": "skipped"}, "reminders_delivery_skip_has_reason"),
        ({"status": "sent", "sent_at": timezone.now()}, "reminders_delivery_sent_has_provider_ref"),
    ],
)
def test_delivery_state_must_be_explainable(org, make_invoice, make_step, fields, constraint):
    with pytest.raises(IntegrityError, match=constraint), transaction.atomic():
        delivery(make_invoice(org), make_step(org), **fields)


@pytest.mark.parametrize("offset_days, allowed", [(-30, True), (-3, True), (0, True), (14, True), (-31, False)])
def test_reminders_may_be_scheduled_before_the_due_date(org, make_step, offset_days, allowed):
    if allowed:
        assert make_step(org, offset_days=offset_days).offset_days == offset_days
    else:
        with pytest.raises(IntegrityError, match="reminders_step_offset_range"), transaction.atomic():
            make_step(org, offset_days=offset_days)


def test_policy_rules(org):
    ReminderPolicy.objects.create(organization=org)
    with pytest.raises(IntegrityError, match="reminders_policy_one_default"), transaction.atomic():
        ReminderPolicy.objects.create(organization=org, name="Second")
    with pytest.raises(IntegrityError, match="reminders_policy_window_valid"), transaction.atomic():
        ReminderPolicy.objects.create(
            organization=org, name="Bad", is_default=False, send_window_start=time(18), send_window_end=time(9)
        )
    assert ReminderPolicy.objects.get(organization=org).is_enabled is False  # opt-in automation


# --- Suppression (FR-010, BR-014) ------------------------------------------


def test_suppression_scope_and_lookup(org, other_org):
    EmailSuppression.objects.create(email="Bounced@Example.com", reason="hard_bounce")
    EmailSuppression.objects.create(organization=org, email="optout@example.com", reason="unsubscribe")

    assert EmailSuppression.is_suppressed(other_org, "bounced@example.com")  # platform-wide
    assert EmailSuppression.is_suppressed(org, "OptOut@example.com")
    assert not EmailSuppression.is_suppressed(other_org, "optout@example.com")  # workspace-only

    with pytest.raises(IntegrityError, match="reminders_suppression_scope_matches_reason"), transaction.atomic():
        EmailSuppression.objects.create(organization=org, email="x@example.com", reason="hard_bounce")
    with pytest.raises(IntegrityError, match="reminders_suppression_org_uniq"), transaction.atomic():
        EmailSuppression.objects.create(organization=org, email="optout@example.com", reason="manual")


def test_lifted_suppression_no_longer_applies(org):
    entry = EmailSuppression.objects.create(organization=org, email="c@example.com", reason="manual")
    entry.lifted_at = timezone.now()
    entry.save()
    assert not EmailSuppression.is_suppressed(org, "c@example.com")
    EmailSuppression.objects.create(organization=org, email="c@example.com", reason="unsubscribe")


# --- Billing and webhooks (FR-014, FR-015, BR-016) -------------------------


def test_one_live_subscription_per_workspace(org):
    plan = Plan.objects.create(code="solo", name="Solo", stripe_price_id="price_1")
    base = {"organization": org, "plan": plan, "stripe_event_created": timezone.now()}
    Subscription.objects.create(stripe_subscription_id="sub_1", status="canceled", **base)
    Subscription.objects.create(stripe_subscription_id="sub_2", status="active", **base)
    with pytest.raises(IntegrityError, match="billing_subscription_one_live_per_org"), transaction.atomic():
        Subscription.objects.create(stripe_subscription_id="sub_3", status="trialing", **base)
    with pytest.raises(IntegrityError), transaction.atomic():
        Subscription.objects.create(stripe_subscription_id="sub_1", status="canceled", **base)


def test_replayed_webhook_is_rejected():
    WebhookEvent.objects.create(provider="stripe", external_id="evt_1", event_type="x", payload={})
    with pytest.raises(IntegrityError, match="webhooks_event_provider_id_uniq"), transaction.atomic():
        WebhookEvent.objects.create(provider="stripe", external_id="evt_1", event_type="x", payload={})
    WebhookEvent.objects.create(provider="email", external_id="evt_1", event_type="x", payload={})


# --- Imports (FR-007) ------------------------------------------------------


def test_imported_row_must_point_to_its_invoice(org):
    batch = ImportBatch.objects.create(
        organization=org, original_filename="a.csv", file_key="imports/a.csv", file_sha256="0" * 64
    )
    with pytest.raises(IntegrityError, match="imports_row_imported_has_invoice"), transaction.atomic():
        ImportRow.objects.create(organization=org, batch=batch, row_number=1, raw_data={}, status="imported")


# --- Audit (security NFR) --------------------------------------------------


def test_audit_events_are_append_only(org):
    event = AuditEvent.objects.create(organization=org, actor_type="system", action="invoice.created")
    with pytest.raises(ProgrammingError, match="append-only"), transaction.atomic():
        AuditEvent.objects.filter(pk=event.pk).update(action="tampered")
    with pytest.raises(ProgrammingError, match="append-only"), transaction.atomic():
        AuditEvent.objects.filter(pk=event.pk).delete()

    # Workspace deletion leaves its audit history in place.
    org.delete()
    assert AuditEvent.objects.filter(pk=event.pk).exists()

    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL invoicepilot.audit_purge = 'on'")
        AuditEvent.objects.filter(pk=event.pk).delete()
    assert not AuditEvent.objects.filter(pk=event.pk).exists()
