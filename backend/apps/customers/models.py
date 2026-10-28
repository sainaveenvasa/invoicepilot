from django.core.validators import MaxLengthValidator
from django.db import models
from django.db.models.functions import Length, Lower
from django.db.models.lookups import LessThanOrEqual

from apps.accounts.models import normalize_email
from apps.core.models import TenantModel, tenant_key

NOTES_MAX_LENGTH = 5000


class Customer(TenantModel):
    """A client of the freelancer: the payer who receives reminders (UR-03).

    Write only through customers.services, which checks permissions, write
    access, the plan limit and ``version``.
    """

    name = models.CharField(max_length=200)
    email = models.EmailField(max_length=254)
    company_name = models.CharField(max_length=200, blank=True)
    notes = models.TextField(blank=True, validators=[MaxLengthValidator(NOTES_MAX_LENGTH)])
    # Customers with invoices are deactivated, never hard-deleted (FR-004);
    # Invoice.customer uses RESTRICT to back that up in the database.
    is_active = models.BooleanField(default=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    # Optimistic lock (database design §11.6): every user edit increments it,
    # and edits that loaded an older version are refused.
    version = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [
            tenant_key("customers_customer_tenant_key"),
            # One customer per email per workspace keeps CSV matching deterministic.
            models.UniqueConstraint(fields=["organization", "email"], name="customers_customer_email_uniq"),
            models.CheckConstraint(condition=models.Q(email=Lower("email")), name="customers_customer_email_lowercase"),
            models.CheckConstraint(
                condition=models.Q(is_active=True, deactivated_at__isnull=True)
                | models.Q(is_active=False, deactivated_at__isnull=False),
                name="customers_customer_deactivation_consistent",
            ),
            # Forms and serializers already refuse these; the database has the last word.
            models.CheckConstraint(condition=~models.Q(name=""), name="customers_customer_name_set"),
            models.CheckConstraint(condition=~models.Q(email=""), name="customers_customer_email_set"),
            models.CheckConstraint(
                condition=LessThanOrEqual(Length("notes"), NOTES_MAX_LENGTH), name="customers_customer_notes_length"
            ),
            models.CheckConstraint(condition=models.Q(version__gte=1), name="customers_customer_version_positive"),
        ]
        indexes = [
            models.Index(fields=["organization", "name"], name="customers_org_name_idx"),
        ]

    def save(self, *args, **kwargs):
        self.email = normalize_email(self.email)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name
