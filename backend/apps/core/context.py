from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RequestContext:
    """Who is acting, and in which workspace (system design §4).

    Built once per request by organizations.middleware.TenantMiddleware.
    Services take the workspace only from ``ctx.organization``, never from an
    argument the caller controls.
    """

    actor_type: str  # "anonymous" | "user" | "worker" | "system"
    source: str  # "web" | "api" | "task" | "command"
    user: Any = None
    organization: Any = None
    membership: Any = None

    @classmethod
    def anonymous(cls, *, source: str = "web") -> "RequestContext":
        return cls(actor_type="anonymous", source=source)

    @classmethod
    def for_user(cls, user, *, membership=None, source: str = "web") -> "RequestContext":
        return cls(
            actor_type="user",
            source=source,
            user=user,
            organization=membership.organization if membership else None,
            membership=membership,
        )
