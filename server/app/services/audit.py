"""Audit napló: minden kézi módosítás nyoma, régi és új értékkel."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AuditLog
from ..timeutil import utcnow


def _default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _dump(payload: dict[str, Any] | None) -> str | None:
    if payload is None:
        return None
    return json.dumps(payload, ensure_ascii=False, default=_default, sort_keys=True)


def record(
    db: Session,
    action: str,
    entity: str,
    entity_id: Any = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    actor: str = "admin",
) -> AuditLog:
    entry = AuditLog(
        ts=utcnow(),
        actor=actor,
        action=action,
        entity=entity,
        entity_id=None if entity_id is None else str(entity_id),
        before_json=_dump(before),
        after_json=_dump(after),
    )
    db.add(entry)
    db.flush()
    return entry


def recent(db: Session, limit: int = 50) -> list[AuditLog]:
    return list(db.scalars(select(AuditLog).order_by(AuditLog.ts.desc()).limit(limit)))


def punch_snapshot(punch) -> dict[str, Any]:  # noqa: ANN001
    """Egy bélyegzés auditálható állapota."""
    return {
        "id": punch.id,
        "event_uuid": punch.event_uuid,
        "card_uid": punch.card_uid,
        "employee_id": punch.employee_id,
        "direction": punch.direction,
        "ts_utc": punch.ts_utc,
        "ts_local": punch.ts_local,
        "note": punch.note,
    }
