from django.conf import settings
from django.db import models
from django.utils.timezone import now

from invoicepilot.core.models import TimeStampedModel, UUIDModel
from invoicepilot.core.validators import CURRENCY_CODE_REGEX, validate_currency, validate_timezone


class Organization(UUIDModel, TimeStampedModel):
    """A workspace: the tenant boundary for every business record (FR-003)."""

    name = models.CharField(max_length=200)
    timezone = models.CharField(max_length=64, default="UTC", validators=[validate_timezone])
    default_currency = models.CharField(max_length=3, validators=[validate_currency])
    stripe_customer_id = models.CharField(max_length=255, unique=True, null=True, blank=True)
    is_active = models.BooleanField(default=True)
    # Set while no subscription grants write access; maintained only by
    # billing.access.sync_workspace_access(). A read-only workspace can view
    # and export its data but cannot change it or send reminders. New
    # workspaces start read-only until checkout completes. Drives the
    # read-only retention period (retention.policy.READ_ONLY_WORKSPACE).
    read_only_since = models.DateTimeField(null=True, blank=True, default=now)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(default_currency__regex=CURRENCY_CODE_REGEX),
                name="organizations_org_currency_format",
            ),
            models.CheckConstraint(condition=~models.Q(timezone=""), name="organizations_org_timezone_set"),
        ]

    def __str__(self):
        return self.name


class Membership(UUIDModel, TimeStampedModel):
    """Grants a user access to a workspace. The MVP has exactly one owner per
    workspace; more roles can be added later without a schema rewrite."""

    class Role(models.TextChoices):
        OWNER = "owner", "Owner"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="memberships")
    # RESTRICT: a user cannot be deleted while they still own a workspace.
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.RESTRICT, related_name="memberships")
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.OWNER)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "user"], name="organizations_membership_uniq"),
            models.UniqueConstraint(
                fields=["organization"],
                condition=models.Q(role="owner"),
                name="organizations_membership_one_owner",
            ),
            models.CheckConstraint(condition=models.Q(role__in=["owner"]), name="organizations_membership_role_valid"),
        ]
