"""Role → action map and the checks every tenant operation runs (system design §6).

The MVP has one role, but every check names an action, so adding roles later
changes this data, not the call sites.
"""

from apps.core import errors

ROLE_PERMISSIONS = {
    "owner": frozenset(
        {
            "workspace.view",
            "workspace.update",
            "customers.view",
            "customers.manage",
            "invoices.view",
            "invoices.manage",
            "invoices.record_payment",
            "invoices.reverse_payment",
            "reminders.view",
            "reminders.manage",
            "reminders.enable_automation",
            "imports.manage",
            "reports.view",
            "reports.export",
            "billing.view",
            "billing.manage",
            "audit.view",
        }
    ),
}

ACTIONS = frozenset().union(*ROLE_PERMISSIONS.values())


def has_permission(ctx, action: str) -> bool:
    if action not in ACTIONS:
        # A typo must fail loudly in tests, not quietly deny in production.
        raise ValueError(f"Unknown action {action!r}")
    membership = ctx.membership
    return membership is not None and action in ROLE_PERMISSIONS.get(membership.role, frozenset())


def require_active_workspace(ctx) -> None:
    """The caller is a member of an active workspace. No membership is a 404,
    never a 403, so a response can't confirm another workspace exists."""
    if ctx.membership is None or ctx.organization is None:
        raise errors.NotFound()
    if not ctx.organization.is_active:
        raise errors.WorkspaceSuspended()


def require(ctx, action: str) -> None:
    require_active_workspace(ctx)
    if not has_permission(ctx, action):
        raise errors.PermissionDenied()


def require_write_access(ctx) -> None:
    """For data-changing actions (decision D3). The flag is maintained by
    billing.access.sync_workspace_access()."""
    if ctx.organization.read_only_since is not None:
        raise errors.ReadOnlyWorkspace()
