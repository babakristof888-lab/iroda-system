"""SQLAlchemy 2.x adatmodell.

Konvenció: minden `DateTime` oszlop **naiv UTC**-t tárol (lásd `timeutil`).
Az egyetlen kivétel a `punches.ts_local`, ami szándékosan a gateway által
küldött nyers szöveg, auditálási célból.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .timeutil import utcnow


class Base(DeclarativeBase):
    pass


class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    employee_code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    cards: Mapped[list["Card"]] = relationship(back_populates="employee")

    def __repr__(self) -> str:  # pragma: no cover - csak hibakereséshez
        return f"<Employee {self.employee_code} {self.name!r}>"


class Card(Base):
    __tablename__ = "cards"

    id: Mapped[int] = mapped_column(primary_key=True)
    uid: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    employee: Mapped["Employee | None"] = relationship(back_populates="cards")


class Punch(Base):
    """Egy nyers bélyegzés. Soha nem törlődik automatikusan."""

    __tablename__ = "punches"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Idempotencia-kulcs: a gateway generálja, a UNIQUE index védi a duplikáció ellen.
    event_uuid: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    card_uid: Mapped[str] = mapped_column(String(64), nullable=False)
    employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    # 'IN' | 'OUT' | None. None = ismeretlen kártya vagy debounce-olt esemény.
    direction: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # Ez alapján rendezünk MINDIG – nem a received_at alapján.
    ts_utc: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # A gateway által küldött nyers ISO string, audit célra.
    ts_local: Mapped[str] = mapped_column(String(64), nullable=False)
    gateway_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=utcnow)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    employee: Mapped["Employee | None"] = relationship()

    __table_args__ = (
        Index("ix_punches_employee_ts", "employee_id", "ts_utc"),
        Index("ix_punches_card_ts", "card_uid", "ts_utc"),
    )


class WorkSession(Base):
    """Egy IN-től a hozzá tartozó OUT-ig tartó munkamenet.

    Származtatott adat: a `punches` táblából bármikor újraépíthető a
    `services.punches.recalculate_directions` segítségével.
    """

    __tablename__ = "work_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), nullable=False
    )
    in_punch_id: Mapped[int | None] = mapped_column(
        ForeignKey("punches.id", ondelete="SET NULL"), nullable=True
    )
    out_punch_id: Mapped[int | None] = mapped_column(
        ForeignKey("punches.id", ondelete="SET NULL"), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 1, ha nem valódi kilépés zárta le, hanem az automatikus napzárás.
    auto_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    employee: Mapped["Employee"] = relationship()

    __table_args__ = (Index("ix_work_sessions_employee_started", "employee_id", "started_at"),)


class EnvReading(Base):
    """A gateway által óránként feltöltött, összesített szenzoradat."""

    __tablename__ = "env_readings"

    id: Mapped[int] = mapped_column(primary_key=True)
    reading_uuid: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    gateway_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sensor_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    period_start_utc: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    period_end_utc: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    temp_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    temp_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    temp_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    hum_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    hum_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    hum_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    press_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=utcnow)

    __table_args__ = (
        Index("ix_env_readings_gateway_period", "gateway_id", "period_start_utc"),
    )


class Gateway(Base):
    __tablename__ = "gateways"

    id: Mapped[int] = mapped_column(primary_key=True)
    gateway_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_queue_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    serial_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    bme_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class AuditLog(Base):
    """Minden kézi (admin által végzett) módosítás nyoma."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    actor: Mapped[str] = mapped_column(String(64), nullable=False, default="admin")
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    before_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_json: Mapped[str | None] = mapped_column(Text, nullable=True)
