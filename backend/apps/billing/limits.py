"""Plan limits (BR-012), enforced server-side before an insert."""

from collections.abc import Callable

from django.db import connection, transaction

from apps.core import errors

from .models import Plan, Subscription

LIMIT_FIELDS = {"customers": "max_customers"}


def current_plan(organization) -> Plan | None:
    """The plan of the subscription that keeps the workspace writable, if any."""
    subscription = (
        Subscription.objects.filter(organization=organization, status__in=Subscription.WRITE_ACCESS_STATUSES)
        .select_related("plan")
        .first()
    )
    return subscription.plan if subscription else None


def check_plan_limit(organization, resource: str, count: Callable[[], int]) -> None:
    """Raise PlanLimitReached if one more ``resource`` would exceed the plan.

    Call inside the transaction that inserts. The workspace's advisory lock
    is held until commit, so two concurrent creates at 99 of 100 can't both
    pass (database design §11.5). ``count`` runs under the lock and returns
    how many the workspace has now. No plan, or a NULL limit, is unlimited.
    """
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("check_plan_limit must run inside the inserting transaction")
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [f"plan-limit:{organization.pk}"])
    plan = current_plan(organization)
    limit = getattr(plan, LIMIT_FIELDS[resource]) if plan else None
    if limit is None:
        return
    current = count()
    if current >= limit:
        raise errors.PlanLimitReached(limit=limit, current=current)
