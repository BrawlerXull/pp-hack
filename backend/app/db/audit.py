from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog


def audit(session: AsyncSession, actor: str, event: str, incident_id: str | None = None, **data: Any) -> None:
    """Append-only audit trail. Caller commits."""
    session.add(AuditLog(actor=actor, event=event, incident_id=incident_id, data=_jsonable(data)))


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_jsonable(v) for v in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)
