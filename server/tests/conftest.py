"""Közös teszt-környezet.

A környezeti változókat MÉG az `app` importja előtt állítjuk be, mert a
konfiguráció a folyamat indulásakor fagy be.
"""

from __future__ import annotations

import os
import tempfile
import uuid as uuid_lib

import bcrypt

ADMIN_PASSWORD = "teszt-jelszo-123"
API_KEY = "teszt-api-kulcs"

_TMP_DIR = tempfile.mkdtemp(prefix="iroda-teszt-")
os.environ["DB_PATH"] = os.path.join(_TMP_DIR, "test.db")
os.environ["GATEWAY_API_KEY"] = API_KEY
os.environ["ADMIN_PASSWORD_HASH"] = bcrypt.hashpw(
    ADMIN_PASSWORD.encode("utf-8"), bcrypt.gensalt(rounds=4)
).decode("ascii")
os.environ["SESSION_SECRET"] = "teszt-session-titok"
os.environ["COOKIE_SECURE"] = "false"
os.environ["TZ"] = "Europe/Budapest"
os.environ["DEBOUNCE_SECONDS"] = "60"
os.environ["AUTO_CLOSE_HOUR"] = "23:59"
os.environ["HOURLY_RATE"] = "1900"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base, Card, Employee  # noqa: E402
from app.timeutil import utcnow  # noqa: E402

GATEWAY_ID = "iroda-teszt"


@pytest.fixture(autouse=True)
def clean_database():
    """Minden teszt üres adatbázissal indul."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session


@pytest.fixture
def gateway(client):
    """Gateway-ként hívó segéd: API kulcsot tesz minden kérésre."""

    class Gateway:
        headers = {"X-API-Key": API_KEY}

        def events(self, events, gateway_id: str = GATEWAY_ID):
            return client.post(
                "/api/v1/events",
                json={"gateway_id": gateway_id, "events": events},
                headers=self.headers,
            )

        def env(self, readings, gateway_id: str = GATEWAY_ID):
            return client.post(
                "/api/v1/env",
                json={"gateway_id": gateway_id, "readings": readings},
                headers=self.headers,
            )

        def heartbeat(self, **payload):
            body = {"gateway_id": GATEWAY_ID, **payload}
            return client.post("/api/v1/heartbeat", json=body, headers=self.headers)

    return Gateway()


@pytest.fixture
def admin(client):
    """Bejelentkezett admin böngésző."""
    response = client.post(
        "/login", data={"password": ADMIN_PASSWORD, "next": "/"}, follow_redirects=False
    )
    assert response.status_code == 303
    return client


# --------------------------------------------------------------------------
# Adat-segédek
# --------------------------------------------------------------------------
def new_uuid() -> str:
    return str(uuid_lib.uuid4())


def make_employee(name: str = "Teszt Elek", code: str = "E001", uid: str | None = "04A1B2C3") -> int:
    """Dolgozó (opcionálisan kártyával). A dolgozó azonosítóját adja vissza."""
    with SessionLocal() as db:
        employee = Employee(name=name, employee_code=code, active=True)
        db.add(employee)
        db.flush()
        if uid:
            db.add(Card(uid=uid, employee_id=employee.id, active=True, assigned_at=utcnow()))
        db.commit()
        return employee.id


def punch_event(uid: str, ts_local: str, event_uuid: str | None = None) -> dict:
    return {
        "event_uuid": event_uuid or new_uuid(),
        "uid": uid,
        "ts_local": ts_local,
    }


def env_reading(
    period_start: str,
    period_end: str,
    reading_uuid: str | None = None,
    **overrides,
) -> dict:
    reading = {
        "reading_uuid": reading_uuid or new_uuid(),
        "sensor_id": "default",
        "period_start": period_start,
        "period_end": period_end,
        "sample_count": 58,
        "temp_avg": 23.41,
        "temp_min": 22.90,
        "temp_max": 24.10,
        "hum_avg": 41.20,
        "hum_min": 39.80,
        "hum_max": 43.00,
        "press_avg": 1013.25,
    }
    reading.update(overrides)
    return reading
