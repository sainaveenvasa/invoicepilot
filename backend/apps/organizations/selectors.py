"""Workspace and membership reads. Access is always resolved from the user's
own memberships, so an ID from another workspace simply isn't found."""

import uuid

from .models import Membership


def _as_uuid(value) -> uuid.UUID | None:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def memberships_for_user(user):
    """Every workspace the user can open, oldest first."""
    return (
        Membership.objects.filter(user=user, user__is_active=True).select_related("organization").order_by("created_at")
    )


def get_membership(*, user, organization_id) -> Membership | None:
    """The user's membership of one workspace, or None if they have none
    (including when the ID is malformed or belongs to someone else)."""
    organization_id = _as_uuid(organization_id)
    if organization_id is None:
        return None
    return memberships_for_user(user).filter(organization_id=organization_id).first()


def get_sole_membership(user) -> Membership | None:
    """The user's membership when they belong to exactly one workspace."""
    memberships = list(memberships_for_user(user)[:2])
    return memberships[0] if len(memberships) == 1 else None
