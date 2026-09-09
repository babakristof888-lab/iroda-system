"""Az API-szerződés Pydantic modelljei.

Fontos tervezési döntés: a batch-ek elemei a burkoló modellben `Any`
típusúak, és elemenként, külön validáljuk őket a service rétegben. Ha a
`list[EventIn]` szerepelne itt, egyetlen hibás elem 422-vel megbuktatná az
egész köteget – a szerződés viszont azt írja elő, hogy a jó elemek akkor is
dolgozódjanak fel, a rossz pedig az `errors` listába kerüljön.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"a(z) {field_name} kötelező, nem üres szöveg")
    return value.strip()


# --------------------------------------------------------------------------
# Bélyegzések
# --------------------------------------------------------------------------
class EventIn(BaseModel):
    """Egy bélyegzés a gateway pufferéből."""

    model_config = ConfigDict(extra="ignore")

    event_uuid: str
    uid: str
    ts_local: str

    @field_validator("event_uuid", "uid", "ts_local", mode="before")
    @classmethod
    def _not_blank(cls, value: Any, info) -> str:  # noqa: ANN001
        return _require_text(value, info.field_name)


class EventBatch(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={
            "example": {
                "gateway_id": "iroda-fszt",
                "events": [
                    {
                        "event_uuid": "3f2a1c48-9d1e-4a3b-8c77-2e5f0b6a1d90",
                        "uid": "04A1B2C3",
                        "ts_local": "2026-09-06T08:31:12+02:00",
                    }
                ],
            }
        },
    )

    gateway_id: str
    events: list[Any] = Field(default_factory=list)

    @field_validator("gateway_id", mode="before")
    @classmethod
    def _gw(cls, value: Any) -> str:
        return _require_text(value, "gateway_id")


class EventError(BaseModel):
    event_uuid: str | None = None
    reason: str


class EventIngestResponse(BaseModel):
    """`accepted` és `duplicates` egyaránt sikeres feldolgozást jelent:
    a gateway mindkét listát törölheti a pufferéből."""

    accepted: list[str] = Field(default_factory=list)
    duplicates: list[str] = Field(default_factory=list)
    errors: list[EventError] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Környezeti mérések
# --------------------------------------------------------------------------
class ReadingIn(BaseModel):
    """Egy óra összesített szenzoradata."""

    model_config = ConfigDict(extra="ignore")

    reading_uuid: str
    sensor_id: str = "default"
    period_start: str
    period_end: str
    sample_count: int
    temp_avg: float | None = None
    temp_min: float | None = None
    temp_max: float | None = None
    hum_avg: float | None = None
    hum_min: float | None = None
    hum_max: float | None = None
    press_avg: float | None = None

    @field_validator("reading_uuid", "period_start", "period_end", mode="before")
    @classmethod
    def _not_blank(cls, value: Any, info) -> str:  # noqa: ANN001
        return _require_text(value, info.field_name)

    @field_validator("sensor_id", mode="before")
    @classmethod
    def _sensor(cls, value: Any) -> str:
        if value is None or (isinstance(value, str) and not value.strip()):
            return "default"
        return _require_text(value, "sensor_id")


class EnvBatch(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={
            "example": {
                "gateway_id": "iroda-fszt",
                "readings": [
                    {
                        "reading_uuid": "8b1c9f2e-0a44-4c31-9f7d-1b2c3d4e5f60",
                        "sensor_id": "default",
                        "period_start": "2026-09-06T08:00:00+02:00",
                        "period_end": "2026-09-06T09:00:00+02:00",
                        "sample_count": 58,
                        "temp_avg": 23.41,
                        "temp_min": 22.90,
                        "temp_max": 24.10,
                        "hum_avg": 41.20,
                        "hum_min": 39.80,
                        "hum_max": 43.00,
                        "press_avg": 1013.25,
                    }
                ],
            }
        },
    )

    gateway_id: str
    readings: list[Any] = Field(default_factory=list)

    @field_validator("gateway_id", mode="before")
    @classmethod
    def _gw(cls, value: Any) -> str:
        return _require_text(value, "gateway_id")


class ReadingError(BaseModel):
    reading_uuid: str | None = None
    reason: str


class EnvIngestResponse(BaseModel):
    accepted: list[str] = Field(default_factory=list)
    duplicates: list[str] = Field(default_factory=list)
    errors: list[ReadingError] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Heartbeat / health
# --------------------------------------------------------------------------
class HeartbeatIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    gateway_id: str
    version: str | None = None
    queue_size: int | None = None
    serial_ok: bool | None = None
    bme_ok: bool | None = None

    @field_validator("gateway_id", mode="before")
    @classmethod
    def _gw(cls, value: Any) -> str:
        return _require_text(value, "gateway_id")


class HeartbeatOut(BaseModel):
    ok: bool = True
    server_time: str


class HealthOut(BaseModel):
    status: Literal["ok", "error"]
    db: Literal["ok", "fail"]
    version: str


# --------------------------------------------------------------------------
# Az admin felület grafikonjához
# --------------------------------------------------------------------------
class EnvSeriesPoint(BaseModel):
    ts: str
    temp_avg: float | None = None
    temp_min: float | None = None
    temp_max: float | None = None
    hum_avg: float | None = None
    hum_min: float | None = None
    hum_max: float | None = None
    press_avg: float | None = None
    sample_count: int


class EnvSeriesResponse(BaseModel):
    hours: int
    sensor_id: str
    points: list[EnvSeriesPoint] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Statisztika grafikonok
# --------------------------------------------------------------------------
class DailyBarOut(BaseModel):
    date: str
    label: str
    weekday: str
    hours: float
    seconds: int
    sessions: int
    auto_closed: bool
    open: bool
    weekend: bool


class DailyStatsResponse(BaseModel):
    employee_id: int
    employee_name: str
    period_label: str
    total_hours: float
    bars: list[DailyBarOut] = Field(default_factory=list)


class EmployeeTotalOut(BaseModel):
    employee_id: int
    name: str
    employee_code: str
    hours: float
    seconds: int
    days: int
    average_hours_per_day: float
    auto_closed_days: int
    open_count: int


class EmployeeStatsResponse(BaseModel):
    period_label: str
    total_hours: float
    employees: list[EmployeeTotalOut] = Field(default_factory=list)
