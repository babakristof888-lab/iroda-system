"""A bélyegzés-végpont szerződése: idempotencia, batch-tűrés, autentikáció."""

from __future__ import annotations

from sqlalchemy import func, select

from app.config import MAX_EVENTS_PER_BATCH
from app.db import SessionLocal
from app.models import Gateway, Punch
from conftest import make_employee, new_uuid, punch_event


def _punch_count(event_uuid: str | None = None) -> int:
    with SessionLocal() as db:
        query = select(func.count(Punch.id))
        if event_uuid:
            query = query.where(Punch.event_uuid == event_uuid)
        return db.scalar(query)


def test_health_nem_igenyel_api_kulcsot(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert body["version"]


def test_hianyzo_es_hibas_api_kulcs_401(client):
    payload = {"gateway_id": "iroda-teszt", "events": []}
    assert client.post("/api/v1/events", json=payload).status_code == 401
    assert (
        client.post("/api/v1/events", json=payload, headers={"X-API-Key": "rossz"}).status_code
        == 401
    )


def test_event_uuid_duplikacio_nem_hoz_letre_masodik_rekordot(gateway):
    make_employee()
    event_uuid = new_uuid()
    event = punch_event("04A1B2C3", "2026-09-01T08:00:00+02:00", event_uuid)

    first = gateway.events([event]).json()
    assert first["accepted"] == [event_uuid]
    assert first["duplicates"] == []

    # A gateway nem kapta meg a választ, ezért újraküldi ugyanazt.
    second = gateway.events([event]).json()
    assert second["accepted"] == []
    assert second["duplicates"] == [event_uuid]
    assert second["errors"] == []

    assert _punch_count(event_uuid) == 1
    assert _punch_count() == 1


def test_duplikatum_ugyanabban_a_kotegben_is_kezelt(gateway):
    make_employee()
    event = punch_event("04A1B2C3", "2026-09-01T08:00:00+02:00")

    result = gateway.events([event, dict(event)]).json()
    assert result["accepted"] == [event["event_uuid"]]
    assert result["duplicates"] == [event["event_uuid"]]
    assert _punch_count() == 1


def test_hibas_esemeny_nem_buktatja_el_a_jokat(gateway):
    make_employee()
    jo_egy = punch_event("04A1B2C3", "2026-09-01T08:00:00+02:00")
    jo_ketto = punch_event("04A1B2C3", "2026-09-01T16:00:00+02:00")
    rossz_datum = punch_event("04A1B2C3", "tegnap reggel")
    hianyos = {"event_uuid": new_uuid(), "ts_local": "2026-09-01T09:00:00+02:00"}  # nincs uid
    ures_uuid = {"event_uuid": "", "uid": "04A1B2C3", "ts_local": "2026-09-01T10:00:00+02:00"}

    result = gateway.events([jo_egy, rossz_datum, hianyos, ures_uuid, jo_ketto]).json()

    assert set(result["accepted"]) == {jo_egy["event_uuid"], jo_ketto["event_uuid"]}
    reasons = {error["reason"] for error in result["errors"]}
    assert reasons == {"invalid_timestamp", "invalid_payload"}
    assert len(result["errors"]) == 3
    assert _punch_count() == 2


def test_hibas_esemeny_uuid_ja_visszakerul_a_valaszba(gateway):
    make_employee()
    rossz = punch_event("04A1B2C3", "nem egy datum")
    result = gateway.events([rossz]).json()
    assert result["errors"] == [
        {"event_uuid": rossz["event_uuid"], "reason": "invalid_timestamp"}
    ]


def test_tul_nagy_koteg_413(gateway):
    events = [
        punch_event("04A1B2C3", "2026-09-01T08:00:00+02:00")
        for _ in range(MAX_EVENTS_PER_BATCH + 1)
    ]
    assert gateway.events(events).status_code == 413
    assert _punch_count() == 0


def test_maximalis_meretu_koteg_meg_atmegy(gateway):
    make_employee()
    events = [
        punch_event("04A1B2C3", f"2026-09-01T08:{index // 60:02d}:{index % 60:02d}+02:00")
        for index in range(MAX_EVENTS_PER_BATCH)
    ]
    response = gateway.events(events)
    assert response.status_code == 200
    assert len(response.json()["accepted"]) == MAX_EVENTS_PER_BATCH


def test_heartbeat_frissiti_a_gateway_tablat(gateway):
    response = gateway.heartbeat(version="1.0.0", queue_size=7, serial_ok=True, bme_ok=False)
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["server_time"]

    with SessionLocal() as db:
        row = db.scalar(select(Gateway).where(Gateway.gateway_id == "iroda-teszt"))
        assert row is not None
        assert row.version == "1.0.0"
        assert row.last_queue_size == 7
        assert row.serial_ok is True
        assert row.bme_ok is False
        assert row.last_seen_at is not None


def test_a_gateway_id_es_a_nyers_idobelyeg_eltarolodik(gateway):
    make_employee()
    event = punch_event("04A1B2C3", "2026-09-01T08:31:12+02:00")
    gateway.events([event], gateway_id="iroda-emelet")

    with SessionLocal() as db:
        punch = db.scalar(select(Punch))
        assert punch.gateway_id == "iroda-emelet"
        # A nyers string auditálás miatt változatlanul megmarad...
        assert punch.ts_local == "2026-09-01T08:31:12+02:00"
        # ...miközben a rendezéshez használt érték UTC.
        assert punch.ts_utc.isoformat() == "2026-09-01T06:31:12"
        assert punch.received_at is not None
