"""Apply the retention policy. Every function is idempotent and safe to re-run."""

from django.core.files.storage import default_storage
from django.db import connection, transaction
from django.db.models import Q

from apps.audit.models import AuditEvent
from apps.imports.models import ImportBatch, ImportRow
from apps.organizations.models import Organization
from apps.webhooks.models import WebhookEvent

from . import policy

AUDIT_CHUNK_SIZE = 5000


def purge_audit_events(now) -> int:
    """Delete audit events older than the policy, in chunks, through the trigger's purge switch."""
    cutoff = now - policy.AUDIT_EVENTS
    deleted = 0
    while True:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SET LOCAL invoicepilot.audit_purge = 'on'")
            ids = list(AuditEvent.objects.filter(created_at__lt=cutoff).values_list("id", flat=True)[:AUDIT_CHUNK_SIZE])
            if ids:
                deleted += AuditEvent.objects.filter(id__in=ids).delete()[0]
            cursor.execute("SET LOCAL invoicepilot.audit_purge = 'off'")
        if len(ids) < AUDIT_CHUNK_SIZE:
            return deleted


def purge_webhook_events(now) -> int:
    settled = Q(
        status__in=[WebhookEvent.Status.PROCESSED, WebhookEvent.Status.IGNORED],
        received_at__lt=now - policy.WEBHOOK_EVENTS_SETTLED,
    )
    expired = Q(received_at__lt=now - policy.WEBHOOK_EVENTS_UNSETTLED)
    return WebhookEvent.objects.filter(settled | expired).delete()[0]


def purge_import_data(now, storage=default_storage) -> int:
    """Delete the uploaded file and row-level data of finished or abandoned batches.

    Finished batches are not modified after reaching a terminal status, so
    updated_at marks when they finished. Batches stuck in `committing` are left
    for investigation.
    """
    expired = ImportBatch.objects.filter(data_purged_at__isnull=True).filter(
        Q(status__in=ImportBatch.TERMINAL_STATUSES, updated_at__lt=now - policy.IMPORT_DATA_AFTER_FINISH)
        | Q(
            status__in=[ImportBatch.Status.UPLOADED, ImportBatch.Status.VALIDATED],
            created_at__lt=now - policy.IMPORT_ABANDONED,
        )
    )
    purged = 0
    for batch in expired.iterator():
        # Outside the transaction: if the database step fails, the next run
        # deletes the (already missing) file again, which storage treats as a no-op.
        storage.delete(batch.file_key)
        with transaction.atomic():
            ImportRow.objects.filter(organization=batch.organization_id, batch=batch).delete()
            if batch.status not in ImportBatch.TERMINAL_STATUSES:
                batch.status = ImportBatch.Status.CANCELLED
            batch.data_purged_at = now
            batch.save(update_fields=["status", "data_purged_at", "updated_at"])
        purged += 1
    return purged


def workspaces_past_retention(now):
    return Organization.objects.filter(read_only_since__lt=now - policy.READ_ONLY_WORKSPACE)


def run(now) -> dict:
    """Run every purge and record the outcome in the audit log."""
    counts = {
        "audit_events_deleted": purge_audit_events(now),
        "webhook_events_deleted": purge_webhook_events(now),
        "import_batches_purged": purge_import_data(now),
        "workspaces_awaiting_deletion": workspaces_past_retention(now).count(),
    }
    AuditEvent.objects.create(actor_type=AuditEvent.ActorType.SYSTEM, action="retention.purged", metadata=counts)
    return counts
