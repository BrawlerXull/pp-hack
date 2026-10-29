"""Who made a dashboard request: the signed-in member's id and role, set by require_owner and read by
audit() and require_manager. Each member has their own id, so the shared demo can say "another visitor
changed this" instead of silently mixing two people's runs."""

from contextvars import ContextVar

_visitor: ContextVar[str | None] = ContextVar("owner_visitor", default=None)
_role: ContextVar[str | None] = ContextVar("member_role", default=None)


def current() -> str | None:
    return _visitor.get()


def role() -> str | None:
    return _role.get()


def set_current(visitor_id: str | None, role: str | None = None) -> None:
    _visitor.set(visitor_id)
    _role.set(role)
