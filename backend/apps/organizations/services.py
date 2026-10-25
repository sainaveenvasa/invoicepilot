from django.db import transaction

from apps.core import errors

from . import permissions
from .models import Membership, Organization

UPDATABLE_FIELDS = ("name", "timezone", "default_currency")


def create_workspace(*, ctx, name: str, timezone: str = "UTC", default_currency: str = "USD") -> Organization:
    """Create a workspace and make the acting user its owner, in one
    transaction, so a workspace never exists without exactly one owner.

    New workspaces start read-only until checkout completes (Organization.read_only_since).
    """
    if ctx.user is None or not ctx.user.is_authenticated:
        raise errors.PermissionDenied()
    organization = Organization(name=name.strip(), timezone=timezone, default_currency=default_currency)
    organization.full_clean()
    with transaction.atomic():
        organization.save()
        Membership.objects.create(organization=organization, user=ctx.user, role=Membership.Role.OWNER)
    return organization


def update_workspace(*, ctx, **changes) -> Organization:
    """Last write wins: the workspace has no version column (API contracts §3)."""
    permissions.require(ctx, "workspace.update")
    permissions.require_write_access(ctx)
    unknown = set(changes) - set(UPDATABLE_FIELDS)
    if unknown:
        raise ValueError(f"Not updatable: {sorted(unknown)}")

    with transaction.atomic():
        organization = Organization.objects.select_for_update().get(pk=ctx.organization.pk)
        for field, value in changes.items():
            setattr(organization, field, value.strip() if field == "name" else value)
        organization.full_clean()
        organization.save(update_fields=[*changes, "updated_at"])
    return organization
