from django.utils import timezone

from .models import Subscription


def has_write_access(organization) -> bool:
    """Checked server-side before every data-changing operation and before sending (BR-012)."""
    return organization.read_only_since is None


def sync_workspace_access(organization, now=None) -> None:
    """Recompute Organization.read_only_since from the workspace's subscriptions.

    Called by the Stripe webhook handler after every subscription change. An
    existing read_only_since is preserved so the retention clock is not reset
    by repeated events.
    """
    writable = Subscription.objects.filter(
        organization=organization, status__in=Subscription.WRITE_ACCESS_STATUSES
    ).exists()
    read_only_since = None if writable else (organization.read_only_since or now or timezone.now())
    if read_only_since != organization.read_only_since:
        organization.read_only_since = read_only_since
        organization.save(update_fields=["read_only_since", "updated_at"])
