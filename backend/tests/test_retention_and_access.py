"""Retention policy purges and read-only workspace access."""

from datetime import timedelta

import pytest
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.billing.access import has_write_access, sync_workspace_access
from apps.billing.models import Plan, Subscription
from apps.imports.models import ImportBatch, ImportRow
from apps.retention import purge
from apps.webhooks.models import WebhookEvent

pytestmark = pytest.mark.django_db

NOW = timezone.now()


def days_ago(days):
    return NOW - timedelta(days=days)


class FakeStorage:
    def __init__(self):
        self.deleted = []

    def delete(self, name):
        self.deleted.append(name)


# --- Purges ----------------------------------------------------------------


def test_audit_events_older_than_one_year_are_purged(org):
    old = AuditEvent.objects.create(organization=org, actor_type="user", action="a", created_at=days_ago(366))
    recent = AuditEvent.objects.create(organization=org, actor_type="user", action="b", created_at=days_ago(364))

    assert purge.purge_audit_events(NOW) == 1
    assert list(AuditEvent.objects.values_list("id", flat=True)) == [recent.id]
    assert not AuditEvent.objects.filter(id=old.id).exists()


def test_audit_purge_switch_is_off_again_afterwards(org):
    purge.purge_audit_events(NOW)
    event = AuditEvent.objects.create(organization=org, actor_type="user", action="a")
    with pytest.raises(Exception, match="append-only"), transaction.atomic():
        AuditEvent.objects.filter(id=event.id).delete()


@pytest.mark.parametrize(
    "status, age_days, kept",
    [
        ("processed", 29, True),
        ("processed", 31, False),
        ("ignored", 31, False),
        ("failed", 31, True),
        ("failed", 91, False),
        ("received", 91, False),
    ],
)
def test_webhook_event_retention(status, age_days, kept):
    WebhookEvent.objects.create(
        provider="stripe", external_id="evt", event_type="x", payload={}, status=status, received_at=days_ago(age_days)
    )
    purge.purge_webhook_events(NOW)
    assert WebhookEvent.objects.exists() is kept


def make_batch(org, status, created_days_ago, updated_days_ago=None, key="imports/file.csv"):
    batch = ImportBatch.objects.create(
        organization=org,
        original_filename="file.csv",
        file_key=key,
        file_sha256="a" * 64,
        status=status,
        committed_at=NOW if status == "committed" else None,
    )
    ImportRow.objects.create(organization=org, batch=batch, row_number=1, raw_data={"email": "c@x.com"}, status="valid")
    ImportBatch.objects.filter(pk=batch.pk).update(
        created_at=days_ago(created_days_ago), updated_at=days_ago(updated_days_ago or created_days_ago)
    )
    return batch


def test_import_files_and_rows_are_purged_after_retention(org):
    finished = make_batch(org, "committed", 40, key="imports/old.csv")
    recent = make_batch(org, "committed", 10, key="imports/recent.csv")
    abandoned = make_batch(org, "validated", 8, key="imports/abandoned.csv")
    fresh_draft = make_batch(org, "validated", 2, key="imports/draft.csv")
    storage = FakeStorage()

    assert purge.purge_import_data(NOW, storage=storage) == 2
    assert sorted(storage.deleted) == ["imports/abandoned.csv", "imports/old.csv"]

    for batch in (finished, abandoned):
        batch.refresh_from_db()
        assert batch.data_purged_at is not None
        assert not batch.rows.exists()
    assert abandoned.status == "cancelled"
    for batch in (recent, fresh_draft):
        assert batch.rows.exists()

    assert purge.purge_import_data(NOW, storage=storage) == 0  # idempotent


def test_unfinished_batch_cannot_be_marked_purged(org):
    batch = make_batch(org, "validated", 1)
    with pytest.raises(IntegrityError, match="imports_batch_purged_only_when_finished"), transaction.atomic():
        ImportBatch.objects.filter(pk=batch.pk).update(data_purged_at=NOW)


def test_purge_command_records_its_run(org, monkeypatch, capsys):
    monkeypatch.setattr(purge, "purge_import_data", lambda now: 0)
    call_command("purge_expired_data")
    event = AuditEvent.objects.get(action="retention.purged")
    assert event.metadata["workspaces_awaiting_deletion"] == 0
    assert "audit_events_deleted: 0" in capsys.readouterr().out


# --- Read-only access without a subscription --------------------------------


@pytest.fixture
def subscribe(org):
    plan = Plan.objects.create(code="solo", name="Solo", stripe_price_id="price_1")

    def subscribe(status, sub_id="sub_1"):
        sub, _ = Subscription.objects.update_or_create(
            stripe_subscription_id=sub_id,
            defaults={"organization": org, "plan": plan, "status": status, "stripe_event_created": NOW},
        )
        sync_workspace_access(org, now=NOW)
        return sub

    return subscribe


def test_new_workspace_is_read_only_until_it_subscribes(org, subscribe):
    assert not has_write_access(org)
    subscribe("active")
    assert has_write_access(org)


@pytest.mark.parametrize(
    "status, writable",
    [
        ("active", True),
        ("past_due", True),
        ("unpaid", False),
        ("paused", False),
        ("canceled", False),
        ("incomplete", False),
    ],
)
def test_write_access_follows_subscription_status(org, subscribe, status, writable):
    subscribe(status)
    assert has_write_access(org) is writable


def test_retention_clock_is_not_reset_by_repeated_events(org, subscribe):
    subscribe("active")
    subscribe("canceled")
    started = org.read_only_since
    sync_workspace_access(org, now=NOW + timedelta(days=5))
    assert org.read_only_since == started


def test_workspaces_become_due_for_deletion_after_90_read_only_days(org, other_org):
    type(org).objects.filter(pk=org.pk).update(read_only_since=days_ago(91))
    type(org).objects.filter(pk=other_org.pk).update(read_only_since=days_ago(89))
    assert list(purge.workspaces_past_retention(NOW)) == [org]
