"""Which dashboard visitor made a request. Every judge logs in with the same owner password, so each login
gets its own random visitor id (in the signed session): the shared demo can then say "another visitor
changed this" instead of silently mixing two people's runs. Set by require_owner, read by audit()."""

from contextvars import ContextVar

_visitor: ContextVar[str | None] = ContextVar("owner_visitor", default=None)


def current() -> str | None:
    return _visitor.get()


def set_current(visitor_id: str | None) -> None:
    _visitor.set(visitor_id)
