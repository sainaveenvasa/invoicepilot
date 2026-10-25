from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.core.models import TimeStampedModel, UUIDModel


class WebhookEvent(UUIDModel, TimeStampedModel):
    """Inbox for signature-verified Stripe and email-provider events.

    The row is inserted before processing; the unique (provider, external_id)
    key turns provider retries into no-ops (BR-016, FR-015). Unverified
    requests are rejected and never stored.
    """

    class Provider(models.TextChoices):
        STRIPE = "stripe", "Stripe"
        EMAIL = "email", "Email provider"

    class Status(models.TextChoices):
        RECEIVED = "received", "Received"
        PROCESSED = "processed", "Processed"
        IGNORED = "ignored", "Ignored"
        FAILED = "failed", "Failed"

    provider = models.CharField(max_length=20, choices=Provider.choices)
    external_id = models.CharField(max_length=255)
    event_type = models.CharField(max_length=100)
    payload = models.JSONField()
    received_at = models.DateTimeField(default=timezone.now)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.RECEIVED)
    attempts = models.PositiveSmallIntegerField(default=0)
    processed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    # Resolved during processing; NULL until then or if no workspace matches.
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["provider", "external_id"], name="webhooks_event_provider_id_uniq"),
        ]
        indexes = [
            models.Index(
                fields=["received_at"], condition=Q(status__in=["received", "failed"]), name="webhooks_pending_idx"
            ),
        ]
