"""A gateway felé nyitott API. Ez a szerződés kötelező – a gateway erre épül."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.orm import Session

from ..config import MAX_EVENTS_PER_BATCH, MAX_READINGS_PER_BATCH, settings
from ..db import check_db, get_db
from ..schemas import (
    DailyBarOut,
    DailyStatsResponse,
    EmployeeStatsResponse,
    EmployeeTotalOut,
    EnvBatch,
    EnvIngestResponse,
    EnvSeriesPoint,
    EnvSeriesResponse,
    EventBatch,
    EventIngestResponse,
    HealthOut,
    HeartbeatIn,
    HeartbeatOut,
)
from ..security import require_admin_or_api_key, require_api_key
from ..services import env_readings as env_service
from ..services import gateways as gateway_service
from ..services import punches as punch_service
from ..services import stats as stats_service
from ..models import Employee
from ..timeutil import to_local, utcnow

log = logging.getLogger("iroda.api")

router = APIRouter(prefix="/api/v1", tags=["gateway"])


@router.get("/health", response_model=HealthOut)
def health(response: Response) -> HealthOut:
    """Railway healthcheck. Nem igényel API kulcsot."""
    if not check_db():
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthOut(status="error", db="fail", version=settings.version)
    return HealthOut(status="ok", db="ok", version=settings.version)


@router.post("/events", response_model=EventIngestResponse)
def post_events(
    payload: EventBatch,
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(require_api_key),
) -> EventIngestResponse:
    """Bélyegzések fogadása.

    Az `accepted` és a `duplicates` egyaránt sikeres feldolgozást jelent: a
    gateway mindkét listát törölheti a pufferéből.
    """
    if len(payload.events) > MAX_EVENTS_PER_BATCH:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Legfeljebb {MAX_EVENTS_PER_BATCH} esemény küldhető egy kötegben",
        )

    gateway_service.touch(db, payload.gateway_id, gateway_service.client_ip(request))
    db.commit()

    result = punch_service.ingest_events(db, payload.gateway_id, payload.events)
    if result.errors:
        log.warning(
            "Batch %s: %d elfogadva, %d duplikátum, %d hibás",
            payload.gateway_id,
            len(result.accepted),
            len(result.duplicates),
            len(result.errors),
        )
    return result


@router.post("/env", response_model=EnvIngestResponse)
def post_env(
    payload: EnvBatch,
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(require_api_key),
) -> EnvIngestResponse:
    """Óránként összesített környezeti mérések fogadása."""
    if len(payload.readings) > MAX_READINGS_PER_BATCH:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Legfeljebb {MAX_READINGS_PER_BATCH} mérés küldhető egy kötegben",
        )

    gateway_service.touch(db, payload.gateway_id, gateway_service.client_ip(request))
    db.commit()

    return env_service.ingest_readings(db, payload.gateway_id, payload.readings)


@router.post("/heartbeat", response_model=HeartbeatOut)
def post_heartbeat(
    payload: HeartbeatIn,
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(require_api_key),
) -> HeartbeatOut:
    gateway_service.record_heartbeat(
        db,
        gateway_id=payload.gateway_id,
        ip=gateway_service.client_ip(request),
        version=payload.version,
        queue_size=payload.queue_size,
        serial_ok=payload.serial_ok,
        bme_ok=payload.bme_ok,
    )
    return HeartbeatOut(ok=True, server_time=to_local(utcnow()).isoformat())


@router.get("/env/series", response_model=EnvSeriesResponse)
def env_series(
    hours: int = Query(default=24, ge=1, le=24 * 90),
    sensor_id: str = Query(default="default"),
    db: Session = Depends(get_db),
    _: str = Depends(require_admin_or_api_key),
) -> EnvSeriesResponse:
    """A grafikon adatforrása. A böngésző hívja, session cookie-val."""
    rows = env_service.readings_since(db, hours, sensor_id)
    points = [
        EnvSeriesPoint(
            ts=to_local(row.period_start_utc).isoformat(),
            temp_avg=row.temp_avg,
            temp_min=row.temp_min,
            temp_max=row.temp_max,
            hum_avg=row.hum_avg,
            hum_min=row.hum_min,
            hum_max=row.hum_max,
            press_avg=row.press_avg,
            sample_count=row.sample_count,
        )
        for row in rows
    ]
    return EnvSeriesResponse(hours=hours, sensor_id=sensor_id, points=points)


# --------------------------------------------------------------------------
# Statisztika a grafikonokhoz (a böngésző hívja, session cookie-val)
# --------------------------------------------------------------------------
@router.get("/stats/daily", response_model=DailyStatsResponse)
def stats_daily(
    employee_id: int = Query(...),
    mode: str = Query(default="month"),
    anchor: str = Query(default=""),
    db: Session = Depends(get_db),
    _: str = Depends(require_admin_or_api_key),
) -> DailyStatsResponse:
    """Egy dolgozó napi óraszáma – az időszak minden napjára, a nullákkal együtt."""
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Nincs ilyen dolgozó")

    period = stats_service.parse_period(mode, anchor)
    bars = stats_service.daily_bars(db, period, employee_id)
    return DailyStatsResponse(
        employee_id=employee_id,
        employee_name=employee.name,
        period_label=period.label,
        total_hours=round(sum(bar.seconds for bar in bars) / 3600, 2),
        bars=[
            DailyBarOut(
                date=bar.local_date.isoformat(),
                label=(
                    f"{bar.local_date.day}. {bar.weekday_short}"
                    if period.mode == "month"
                    else f"{bar.weekday} {bar.local_date.day}."
                ),
                weekday=bar.weekday,
                hours=bar.hours,
                seconds=bar.seconds,
                sessions=bar.sessions,
                auto_closed=bar.auto_closed,
                open=bar.open,
                weekend=bar.is_weekend,
            )
            for bar in bars
        ],
    )


@router.get("/stats/employees", response_model=EmployeeStatsResponse)
def stats_employees(
    mode: str = Query(default="month"),
    anchor: str = Query(default=""),
    db: Session = Depends(get_db),
    _: str = Depends(require_admin_or_api_key),
) -> EmployeeStatsResponse:
    """Ki mennyit volt bent összesen az időszakban, csökkenő sorrendben."""
    period = stats_service.parse_period(mode, anchor)
    totals = stats_service.employee_totals(db, period)
    return EmployeeStatsResponse(
        period_label=period.label,
        total_hours=round(sum(total.seconds for total in totals) / 3600, 2),
        employees=[
            EmployeeTotalOut(
                employee_id=total.employee_id,
                name=total.name,
                employee_code=total.employee_code,
                hours=total.hours,
                seconds=total.seconds,
                days=total.day_count,
                average_hours_per_day=total.average_hours_per_day,
                auto_closed_days=total.auto_closed_day_count,
                open_count=total.open_count,
            )
            for total in totals
        ],
    )
