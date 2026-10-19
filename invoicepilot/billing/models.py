from django.db import models
from django.db.models import Q

from invoicepilot.core.models import TenantModel, TimeStampedModel, UUIDModel


class Plan(UUIDModel, TimeStampedModel):
    """A sellable InvoicePilot plan (platform-wide, not tenant-owned).

    Limits are enforced server-side (BR-012); NULL means unlimited.
    """

    code = models.SlugField(max_length=50, unique=True)
    name = models.CharField(max_length=100)
    stripe_price_id = models.CharField(max_length=255, unique=True)
    max_customers = models.PositiveIntegerField(null=True, blank=True)
    max_invoices_per_month = models.PositiveIntegerField(null=True, blank=True)
    max_reminders_per_month = models.PositiveIntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class Subscription(TenantModel):
    """Local mirror of a Stripe subscription, written only by verified webhooks (FR-014, FR-015)."""

    class Status(models.TextChoices):
        INCOMPLETE = "incomplete", "Incomplete"
        INCOMPLETE_EXPIRED = "incomplete_expired", "Incomplete, expired"
        TRIALING = "trialing", "Trialing"
        ACTIVE = "active", "Active"
        PAST_DUE = "past_due", "Past due"
        UNPAID = "unpaid", "Unpaid"
        PAUSED = "paused", "Paused"
        CANCELED = "canceled", "Canceled"

    LIVE_STATUSES = [Status.INCOMPLETE, Status.TRIALING, Status.ACTIVE, Status.PAST_DUE, Status.UNPAID, Status.PAUSED]
    # States that keep a workspace writable. past_due stays writable while
    # Stripe retries the card; once retries are exhausted Stripe moves the
    # subscription to unpaid or canceled and the workspace turns read-only.
    # No trial is offered today; trialing only applies if one is configured in Stripe later.
    WRITE_ACCESS_STATUSES = [Status.ACTIVE, Status.TRIALING, Status.PAST_DUE]

    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="subscriptions")
    # Unique so a replayed "subscription created" event updates, never duplicates.
    stripe_subscription_id = models.CharField(max_length=255, unique=True)
    status = models.CharField(max_length=30, choices=Status.choices)
    current_period_start = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)
    canceled_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    trial_end = models.DateTimeField(null=True, blank=True)
    # `created` of the last Stripe event applied; older events are ignored so
    # out-of-order webhook delivery cannot roll state backwards.
    stripe_event_created = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization"],
                condition=Q(status__in=["incomplete", "trialing", "active", "past_due", "unpaid", "paused"]),
                name="billing_subscription_one_live_per_org",
            ),
        ]
