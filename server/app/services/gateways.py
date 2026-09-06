"""Gateway-nyilvántartás és állapotfigyelés."""

from __future__ import annotations

from datetime import timedelta

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Gateway
from ..timeutil import utcnow


def client_ip(request: Request) -> str | None:
    """A valódi kliens IP – Railway-en proxy mögött futunk."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def touch(db: Session, gateway_id: str, ip: str | None = None) -> Gateway:
    """Minden gateway-hívás frissíti a "legutóbb láttuk" időpontot."""
    row = db.scalar(select(Gateway).where(Gateway.gateway_id == gateway_id))
    if row is None:
        row = Gateway(gateway_id=gateway_id)
        db.add(row)
    row.last_seen_at = utcnow()
    if ip:
        row.last_ip = ip
    return row


def record_heartbeat(
    db: Session,
    gateway_id: str,
    ip: str | None,
    version: str | None,
    queue_size: int | None,
    serial_ok: bool | None,
    bme_ok: bool | None,
) -> Gateway:
    row = touch(db, gateway_id, ip)
    row.version = version
    row.last_queue_size = queue_size
    row.serial_ok = serial_ok
    row.bme_ok = bme_ok
    db.commit()
    return row


def status_list(db: Session) -> list[dict]:
    """A dashboard gateway-táblájának adatai, online/offline besorolással."""
    now = utcnow()
    threshold = timedelta(minutes=max(settings.gateway_offline_minutes, 1))
    rows = db.scalars(select(Gateway).order_by(Gateway.gateway_id)).all()

    result = []
    for row in rows:
        age = None if row.last_seen_at is None else now - row.last_seen_at
        online = age is not None and age <= threshold
        result.append(
            {
                "gateway_id": row.gateway_id,
                "online": online,
                "last_seen_at": row.last_seen_at,
                "minutes_ago": None if age is None else int(age.total_seconds() // 60),
                "last_ip": row.last_ip,
                "version": row.version,
                "last_queue_size": row.last_queue_size,
                "serial_ok": row.serial_ok,
                "bme_ok": row.bme_ok,
            }
        )
    return result
