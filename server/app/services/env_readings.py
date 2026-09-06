"""Környezeti mérések feldolgozása és lekérdezése.

A gateway percenként mér, de **óránként egyszer** küld: az adott óra átlagát,
minimumát és maximumát. Egy sor = egy óra, így a tábla évente ~8760 sorral nő.
Automatikus törlés nincs rajta.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Iterable

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import HUM_RANGE, PRESS_RANGE, TEMP_RANGE, settings
from ..models import EnvReading
from ..schemas import EnvIngestResponse, ReadingError, ReadingIn
from ..timeutil import parse_iso_to_utc, utcnow

log = logging.getLogger("iroda.env")

MIN_VALID_TS = datetime(2020, 1, 1)
MAX_FUTURE_SKEW = timedelta(days=1)


def _in_range(value: float | None, bounds: tuple[float, float]) -> bool:
    return value is None or bounds[0] <= value <= bounds[1]


def _ranges_ok(reading: ReadingIn) -> bool:
    """A BME280 fizikai tartományain kívüli érték hibás mérést jelent."""
    return (
        all(_in_range(v, TEMP_RANGE) for v in (reading.temp_avg, reading.temp_min, reading.temp_max))
        and all(_in_range(v, HUM_RANGE) for v in (reading.hum_avg, reading.hum_min, reading.hum_max))
        and _in_range(reading.press_avg, PRESS_RANGE)
    )


def _validate_period(raw: str) -> datetime:
    ts = parse_iso_to_utc(raw)
    if ts < MIN_VALID_TS or ts > utcnow() + MAX_FUTURE_SKEW:
        raise ValueError("az időbélyeg hihetetlen tartományban van")
    return ts


def ingest_readings(
    db: Session, gateway_id: str, raw_readings: Iterable[Any]
) -> EnvIngestResponse:
    """Egy batch óraösszesítő feldolgozása – ugyanaz a logika, mint az eseményeknél."""
    accepted: list[str] = []
    duplicates: list[str] = []
    errors: list[ReadingError] = []

    for raw in raw_readings:
        uuid_hint = raw.get("reading_uuid") if isinstance(raw, dict) else None
        uuid_hint = uuid_hint if isinstance(uuid_hint, str) else None

        try:
            reading = ReadingIn.model_validate(raw)
        except ValidationError:
            errors.append(ReadingError(reading_uuid=uuid_hint, reason="invalid_payload"))
            continue

        if reading.sample_count < 1:
            errors.append(
                ReadingError(reading_uuid=reading.reading_uuid, reason="invalid_sample_count")
            )
            continue

        try:
            period_start = _validate_period(reading.period_start)
            period_end = _validate_period(reading.period_end)
        except ValueError:
            errors.append(
                ReadingError(reading_uuid=reading.reading_uuid, reason="invalid_timestamp")
            )
            continue

        if period_end <= period_start:
            errors.append(ReadingError(reading_uuid=reading.reading_uuid, reason="invalid_period"))
            continue

        if not _ranges_ok(reading):
            errors.append(ReadingError(reading_uuid=reading.reading_uuid, reason="out_of_range"))
            continue

        existing = db.scalar(
            select(EnvReading.id).where(EnvReading.reading_uuid == reading.reading_uuid)
        )
        if existing is not None:
            duplicates.append(reading.reading_uuid)
            continue

        row = EnvReading(
            reading_uuid=reading.reading_uuid,
            gateway_id=gateway_id,
            sensor_id=reading.sensor_id,
            period_start_utc=period_start,
            period_end_utc=period_end,
            sample_count=reading.sample_count,
            temp_avg=reading.temp_avg,
            temp_min=reading.temp_min,
            temp_max=reading.temp_max,
            hum_avg=reading.hum_avg,
            hum_min=reading.hum_min,
            hum_max=reading.hum_max,
            press_avg=reading.press_avg,
            received_at=utcnow(),
        )
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError:
            duplicates.append(reading.reading_uuid)
            continue

        accepted.append(reading.reading_uuid)

    db.commit()
    return EnvIngestResponse(accepted=accepted, duplicates=duplicates, errors=errors)


# --------------------------------------------------------------------------
# Lekérdezések az admin felülethez
# --------------------------------------------------------------------------
def latest_reading(db: Session, sensor_id: str = "default") -> EnvReading | None:
    return db.scalar(
        select(EnvReading)
        .where(EnvReading.sensor_id == sensor_id)
        .order_by(EnvReading.period_start_utc.desc())
        .limit(1)
    )


def readings_since(
    db: Session, hours: int, sensor_id: str = "default"
) -> list[EnvReading]:
    since = utcnow() - timedelta(hours=max(hours, 1))
    return list(
        db.scalars(
            select(EnvReading)
            .where(EnvReading.sensor_id == sensor_id, EnvReading.period_start_utc >= since)
            .order_by(EnvReading.period_start_utc.asc())
        )
    )


def known_sensor_ids(db: Session) -> list[str]:
    return list(db.scalars(select(EnvReading.sensor_id).distinct().order_by(EnvReading.sensor_id)))


# --------------------------------------------------------------------------
# Riasztás
# --------------------------------------------------------------------------
def evaluate_alerts(reading: EnvReading | None) -> list[str]:
    """Küszöbön kívüli átlagértékek szöveges figyelmeztetései.

    Csak a felületen jelenik meg – e-mailt, SMS-t, push értesítést nem küldünk.
    """
    if reading is None or not settings.env_alerts_enabled:
        return []

    alerts: list[str] = []
    if reading.temp_avg is not None:
        if reading.temp_avg < settings.temp_min_alert:
            alerts.append(
                f"Hideg van: {reading.temp_avg:.1f} °C "
                f"(alsó küszöb {settings.temp_min_alert:.0f} °C)"
            )
        elif reading.temp_avg > settings.temp_max_alert:
            alerts.append(
                f"Meleg van: {reading.temp_avg:.1f} °C "
                f"(felső küszöb {settings.temp_max_alert:.0f} °C)"
            )
    if reading.hum_avg is not None:
        if reading.hum_avg < settings.hum_min_alert:
            alerts.append(
                f"Száraz a levegő: {reading.hum_avg:.1f} % "
                f"(alsó küszöb {settings.hum_min_alert:.0f} %)"
            )
        elif reading.hum_avg > settings.hum_max_alert:
            alerts.append(
                f"Párás a levegő: {reading.hum_avg:.1f} % "
                f"(felső küszöb {settings.hum_max_alert:.0f} %)"
            )
    return alerts
