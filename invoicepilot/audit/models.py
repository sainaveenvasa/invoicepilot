from django.conf import settings
from django.db import models
from django.db.models.functions import Now


class AuditEvent(models.Model):
    """Append-only record of security- and money-relevant actions.

    A database trigger rejects UPDATE and DELETE (migration 0002). The
    organization and actor links carry no FK constraint and are never
    cascaded, so deleting a workspace or user cannot rewrite history; the
    retention job purges rows explicitly (see 0002_append_only_trigger).
    """

    class ActorType(models.TextChoices):
        USER = "user", "Workspace user"
        ADMIN = "admin", "Platform administrator"
        WORKER = "worker", "Background worker"
        WEBHOOK = "webhook", "External webhook"
        SYSTEM = "system", "System"

    id = models.BigAutoField(primary_key=True)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.DO_NOTHING,
        db_constraint=False,
        null=True,
        blank=True,
        related_name="+",
    )
    actor_type = models.CharField(max_length=20, choices=ActorType.choices)
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.DO_NOTHING,
        db_constraint=False,
        null=True,
        blank=True,
        related_name="+",
    )
    # Dotted verb, e.g. "invoice.status_changed", "admin.support_access".
    action = models.CharField(max_length=100)
    target_type = models.CharField(max_length=100, blank=True)
    target_id = models.UUIDField(null=True, blank=True)
    # {"field": [old, new]} for changed fields only; no secrets or card data.
    changes = models.JSONField(default=dict, blank=True)
    # request_id, ip_address, support-access justification, task id, etc.
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(db_default=Now())

    class Meta:
        indexes = [
            models.Index(fields=["organization", "-created_at"], name="audit_org_created_idx"),
            models.Index(fields=["target_type", "target_id"], name="audit_target_idx"),
        ]
