from django.db import models

from .ids import uuid7


class UUIDModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)

    class Meta:
        abstract = True


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class TenantQuerySet(models.QuerySet):
    def for_organization(self, organization):
        """The only sanctioned entry point for reading tenant data in views and services."""
        return self.filter(organization=organization)


class TenantModel(UUIDModel, TimeStampedModel):
    """A row owned by exactly one workspace (Organization).

    Every tenant-owned table carries a non-null organization_id, even when it
    could be reached through a parent row. That keeps every query scopable by a
    single indexed column and lets the database reject cross-tenant links via
    composite foreign keys (see core.migration_operations.AddTenantForeignKey).
    """

    organization = models.ForeignKey("organizations.Organization", on_delete=models.CASCADE, related_name="+")

    objects = TenantQuerySet.as_manager()

    class Meta:
        abstract = True


def tenant_key(name: str) -> models.UniqueConstraint:
    """UNIQUE (organization_id, id): the target of child tables' composite tenant FKs."""
    return models.UniqueConstraint(fields=["organization", "id"], name=name)
