from datetime import date, time

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Lower

from invoicepilot.accounts.models import normalize_email
from invoicepilot.core.models import TenantModel, TimeStampedModel, UUIDModel, tenant_key


class SenderIdentity(TenantModel):
    """Who a reminder appears to come from. Mail is sent from the platform
    domain with this display name and Reply-To; sending stays disabled until
    the reply-to address is verified (FR-010, BR-007)."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending verification"
        VERIFIED = "verified", "Verified"
        REVOKED = "revoked", "Revoked"

    from_name = models.CharField(max_length=100)
    reply_to_email = models.EmailField(max_length=254)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    verified_at = models.DateTimeField(null=True, blank=True)
    is_default = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "sender identities"
        constraints = [
            tenant_key("reminders_senderidentity_tenant_key"),
            models.UniqueConstraint(fields=["organization", "reply_to_email"], name="reminders_sender_email_uniq"),
            models.UniqueConstraint(
                fields=["organization"], condition=Q(is_default=True), name="reminders_sender_one_default"
            ),
            models.CheckConstraint(
                condition=Q(reply_to_email=Lower("reply_to_email")), name="reminders_sender_email_lowercase"
            ),
            models.CheckConstraint(
                condition=~Q(status="verified") | Q(verified_at__isnull=False),
                name="reminders_sender_verified_at_set",
            ),
        ]

    def save(self, *args, **kwargs):
        self.reply_to_email = normalize_email(self.reply_to_email)
        super().save(*args, **kwargs)


class EmailTemplate(TenantModel):
    """Reminder wording. Placeholders are rendered from a fixed allow-list of
    invoice fields; each workspace gets editable copies of the system defaults."""

    name = models.CharField(max_length=100)
    subject = models.CharField(max_length=200)
    body_text = models.TextField()

    class Meta:
        constraints = [
            tenant_key("reminders_emailtemplate_tenant_key"),
            models.UniqueConstraint(fields=["organization", "name"], name="reminders_template_name_uniq"),
        ]


class ReminderPolicy(TenantModel):
    """When reminders go out (FR-008). Automation is off until the user turns it on."""

    name = models.CharField(max_length=100, default="Default")
    is_enabled = models.BooleanField(default=False)
    is_default = models.BooleanField(default=True)
    # Local times in Organization.timezone (BR-006).
    send_window_start = models.TimeField(default=time(9, 0))
    send_window_end = models.TimeField(default=time(17, 0))
    send_on_weekends = models.BooleanField(default=False)

    class Meta:
        verbose_name_plural = "reminder policies"
        constraints = [
            tenant_key("reminders_reminderpolicy_tenant_key"),
            models.UniqueConstraint(fields=["organization", "name"], name="reminders_policy_name_uniq"),
            models.UniqueConstraint(
                fields=["organization"], condition=Q(is_default=True), name="reminders_policy_one_default"
            ),
            models.CheckConstraint(
                condition=Q(send_window_start__lt=F("send_window_end")), name="reminders_policy_window_valid"
            ),
        ]


class ReminderStep(TenantModel):
    """One reminder in a policy's sequence; the default sequence is +3, +7 and +14 days."""

    policy = models.ForeignKey(ReminderPolicy, on_delete=models.CASCADE, related_name="steps")
    position = models.PositiveSmallIntegerField()
    # Days relative to the due date: positive is overdue, negative is a heads-up before it.
    offset_days = models.SmallIntegerField()
    template = models.ForeignKey(EmailTemplate, on_delete=models.RESTRICT, related_name="+")
    # Steps with delivery history are deactivated, not deleted (ReminderDelivery.step is RESTRICT).
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["position"]
        constraints = [
            tenant_key("reminders_reminderstep_tenant_key"),
            models.UniqueConstraint(fields=["policy", "position"], name="reminders_step_position_uniq"),
            models.UniqueConstraint(fields=["policy", "offset_days"], name="reminders_step_offset_uniq"),
            models.CheckConstraint(
                condition=Q(offset_days__gte=-30, offset_days__lte=365), name="reminders_step_offset_range"
            ),
        ]


class ReminderDelivery(TenantModel):
    """One logical reminder: invoice x step x the due date it was computed from.

    That triple is unique (BR-008), so a re-run scheduler cannot insert a
    second row; it can only advance this row's state. Retries are
    ReminderDeliveryAttempt rows. Moving the due date produces a new logical
    reminder, and the old row is cancelled with reason `schedule_changed` (BR-009).
    """

    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        QUEUED = "queued", "Queued"
        SENDING = "sending", "Sending"
        SENT = "sent", "Accepted by provider"
        DELIVERED = "delivered", "Delivered"
        BOUNCED = "bounced", "Bounced"
        COMPLAINED = "complained", "Marked as spam"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"
        CANCELLED = "cancelled", "Cancelled"

    class SkipReason(models.TextChoices):
        INVOICE_PAID = "invoice_paid", "Invoice is paid"
        INVOICE_DISPUTED = "invoice_disputed", "Invoice is disputed"
        INVOICE_PAUSED = "invoice_paused", "Reminders paused for invoice"
        INVOICE_VOID = "invoice_void", "Invoice is void"
        POLICY_DISABLED = "policy_disabled", "Automation disabled"
        STEP_INACTIVE = "step_inactive", "Reminder step disabled"
        SENDER_UNVERIFIED = "sender_unverified", "Sender identity not verified"
        RECIPIENT_SUPPRESSED = "recipient_suppressed", "Recipient suppressed or opted out"
        CUSTOMER_INACTIVE = "customer_inactive", "Customer deactivated"
        SUBSCRIPTION_INACTIVE = "subscription_inactive", "Subscription inactive"
        PLAN_LIMIT = "plan_limit", "Plan sending limit reached"
        SCHEDULE_CHANGED = "schedule_changed", "Due date or policy changed"

    SENT_STATUSES = [Status.SENT, Status.DELIVERED, Status.BOUNCED, Status.COMPLAINED]

    invoice = models.ForeignKey("invoices.Invoice", on_delete=models.RESTRICT, related_name="reminder_deliveries")
    step = models.ForeignKey(ReminderStep, on_delete=models.RESTRICT, related_name="deliveries")
    sender_identity = models.ForeignKey(
        SenderIdentity, on_delete=models.RESTRICT, null=True, blank=True, related_name="+"
    )
    invoice_due_date = models.DateField()
    # Deterministic: also sent to the email provider as its idempotency key.
    idempotency_key = models.CharField(max_length=128, unique=True)
    scheduled_for = models.DateTimeField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SCHEDULED)
    skip_reason = models.CharField(max_length=40, choices=SkipReason.choices, blank=True)
    # Snapshot of exactly what was sent, rendered immediately before sending.
    recipient_email = models.EmailField(max_length=254, blank=True)
    subject = models.CharField(max_length=255, blank=True)
    body_text = models.TextField(blank=True)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    provider_message_id = models.CharField(max_length=255, unique=True, null=True, blank=True)
    queued_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    class Meta:
        verbose_name_plural = "reminder deliveries"
        constraints = [
            tenant_key("reminders_reminderdelivery_tenant_key"),
            models.UniqueConstraint(
                fields=["invoice", "step", "invoice_due_date"], name="reminders_delivery_logical_uniq"
            ),
            models.CheckConstraint(
                condition=~Q(status__in=["skipped", "cancelled"]) | ~Q(skip_reason=""),
                name="reminders_delivery_skip_has_reason",
            ),
            models.CheckConstraint(
                condition=~Q(status__in=["sent", "delivered", "bounced", "complained"])
                | Q(sent_at__isnull=False, provider_message_id__isnull=False),
                name="reminders_delivery_sent_has_provider_ref",
            ),
        ]
        indexes = [
            # The scheduler's "what is due now" scan.
            models.Index(fields=["scheduled_for"], condition=Q(status="scheduled"), name="reminders_delivery_due_idx"),
            # Activity page (FR-011).
            models.Index(fields=["organization", "-scheduled_for"], name="reminders_delivery_org_idx"),
            # Monthly sending-limit counts (BR-012, BR-014).
            models.Index(
                fields=["organization", "sent_at"], condition=Q(sent_at__isnull=False), name="reminders_delivery_sent_idx"
            ),
        ]

    @staticmethod
    def build_idempotency_key(invoice_id, step_id, due_date: date) -> str:
        return f"reminder:{invoice_id}:{step_id}:{due_date.isoformat()}"


class ReminderDeliveryAttempt(TenantModel):
    """One call to the email provider for a delivery (FR-011: retries are recorded)."""

    class Outcome(models.TextChoices):
        IN_PROGRESS = "in_progress", "In progress"
        ACCEPTED = "accepted", "Accepted"
        TRANSIENT_ERROR = "transient_error", "Transient error, will retry"
        PERMANENT_ERROR = "permanent_error", "Permanent error"

    delivery = models.ForeignKey(ReminderDelivery, on_delete=models.CASCADE, related_name="attempts")
    attempt_number = models.PositiveSmallIntegerField()
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(max_length=20, choices=Outcome.choices, default=Outcome.IN_PROGRESS)
    provider_status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    error_message = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["delivery", "attempt_number"], name="reminders_attempt_number_uniq"),
            models.CheckConstraint(condition=Q(attempt_number__gte=1), name="reminders_attempt_number_positive"),
        ]


class EmailSuppression(UUIDModel, TimeStampedModel):
    """Addresses that must not receive reminders (FR-010, BR-014).

    Hard bounces and spam complaints are platform-wide (organization is NULL)
    because they affect the shared sending reputation. Unsubscribes and manual
    blocks apply to one workspace only.
    """

    class Reason(models.TextChoices):
        HARD_BOUNCE = "hard_bounce", "Hard bounce"
        COMPLAINT = "complaint", "Spam complaint"
        UNSUBSCRIBE = "unsubscribe", "Recipient opted out"
        MANUAL = "manual", "Blocked by user"

    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, null=True, blank=True, related_name="+"
    )
    email = models.EmailField(max_length=254)
    reason = models.CharField(max_length=20, choices=Reason.choices)
    source = models.CharField(max_length=50, blank=True)
    lifted_at = models.DateTimeField(null=True, blank=True)
    lifted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["email"],
                condition=Q(organization__isnull=True, lifted_at__isnull=True),
                name="reminders_suppression_global_uniq",
            ),
            models.UniqueConstraint(
                fields=["organization", "email"],
                condition=Q(organization__isnull=False, lifted_at__isnull=True),
                name="reminders_suppression_org_uniq",
            ),
            models.CheckConstraint(condition=Q(email=Lower("email")), name="reminders_suppression_email_lowercase"),
            models.CheckConstraint(
                condition=Q(reason__in=["hard_bounce", "complaint"], organization__isnull=True)
                | Q(reason__in=["unsubscribe", "manual"], organization__isnull=False),
                name="reminders_suppression_scope_matches_reason",
            ),
        ]

    def save(self, *args, **kwargs):
        self.email = normalize_email(self.email)
        super().save(*args, **kwargs)

    @classmethod
    def is_suppressed(cls, organization, email: str) -> bool:
        return cls.objects.filter(
            Q(organization=organization) | Q(organization__isnull=True),
            email=normalize_email(email),
            lifted_at__isnull=True,
        ).exists()
