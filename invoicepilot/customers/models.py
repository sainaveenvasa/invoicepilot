from django.db import models
from django.db.models.functions import Lower

from invoicepilot.accounts.models import normalize_email
from invoicepilot.core.models import TenantModel, tenant_key


class Customer(TenantModel):
    """A client of the freelancer: the payer who receives reminders (UR-03)."""

    name = models.CharField(max_length=200)
    email = models.EmailField(max_length=254)
    company_name = models.CharField(max_length=200, blank=True)
    notes = models.TextField(blank=True)
    # Customers with invoices are deactivated, never hard-deleted (FR-004);
    # Invoice.customer uses RESTRICT to back that up in the database.
    is_active = models.BooleanField(default=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)

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
        ]
        indexes = [
            models.Index(fields=["organization", "name"], name="customers_org_name_idx"),
        ]

    def save(self, *args, **kwargs):
        self.email = normalize_email(self.email)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name
