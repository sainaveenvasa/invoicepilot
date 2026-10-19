from django.conf import settings
from django.db import models
from django.db.models import Q

from invoicepilot.core.models import TenantModel, tenant_key


class ImportBatch(TenantModel):
    """One uploaded CSV file moving through preview -> commit (FR-007).

    Committing never enables reminder automation (BR-013).
    """

    class Status(models.TextChoices):
        UPLOADED = "uploaded", "Uploaded"
        VALIDATED = "validated", "Validated, awaiting review"
        COMMITTING = "committing", "Committing"
        COMMITTED = "committed", "Committed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    original_filename = models.CharField(max_length=255)
    # Object-storage key: uploads must not live only on a container filesystem.
    file_key = models.CharField(max_length=512)
    file_sha256 = models.CharField(max_length=64)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.UPLOADED)
    row_count = models.PositiveIntegerField(default=0)
    valid_row_count = models.PositiveIntegerField(default=0)
    error_row_count = models.PositiveIntegerField(default=0)
    duplicate_row_count = models.PositiveIntegerField(default=0)
    committed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    # When the uploaded file and its row-level data were deleted under the
    # retention policy. The batch row itself (counts, filename, hash) is kept.
    data_purged_at = models.DateTimeField(null=True, blank=True)

    TERMINAL_STATUSES = [Status.COMMITTED, Status.FAILED, Status.CANCELLED]

    class Meta:
        constraints = [
            tenant_key("imports_importbatch_tenant_key"),
            models.CheckConstraint(
                condition=Q(file_sha256__regex=r"^[0-9a-f]{64}$"), name="imports_batch_sha256_format"
            ),
            models.CheckConstraint(
                condition=~Q(status="committed") | Q(committed_at__isnull=False),
                name="imports_batch_committed_at_set",
            ),
            models.CheckConstraint(
                condition=Q(data_purged_at__isnull=True) | Q(status__in=["committed", "failed", "cancelled"]),
                name="imports_batch_purged_only_when_finished",
            ),
        ]
        indexes = [
            # "This file was already imported on ..." warning on re-upload.
            models.Index(fields=["organization", "file_sha256"], name="imports_batch_org_sha_idx"),
        ]


class ImportRow(TenantModel):
    """One CSV row with its row-level validation result, shown in the preview."""

    class Status(models.TextChoices):
        VALID = "valid", "Valid"
        ERROR = "error", "Error"
        DUPLICATE = "duplicate", "Duplicate invoice number"
        IMPORTED = "imported", "Imported"
        SKIPPED = "skipped", "Skipped"

    batch = models.ForeignKey(ImportBatch, on_delete=models.CASCADE, related_name="rows")
    row_number = models.PositiveIntegerField()
    raw_data = models.JSONField()
    errors = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices)
    invoice = models.ForeignKey(
        "invoices.Invoice", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["batch", "row_number"], name="imports_row_number_uniq"),
            models.CheckConstraint(
                condition=~Q(status="imported") | Q(invoice__isnull=False),
                name="imports_row_imported_has_invoice",
            ),
        ]
