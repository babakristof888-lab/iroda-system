"""A környezeti mérések végpontja: idempotencia és tartomány-validáció."""

from __future__ import annotations

from sqlalchemy import func, select

from app.config import MAX_READINGS_PER_BATCH
from app.db import SessionLocal
from app.models import EnvReading
from app.services import env_readings as env_service
from conftest import env_reading, new_uuid

START = "2026-09-01T08:00:00+02:00"
END = "2026-09-01T09:00:00+02:00"


def _reading_count() -> int:
    with SessionLocal() as db:
        return db.scalar(select(func.count(EnvReading.id)))


def test_ervenyes_meres_mentodik(gateway):
    reading = env_reading(START, END)
    result = gateway.env([reading]).json()

    assert result["accepted"] == [reading["reading_uuid"]]
    assert result["duplicates"] == []
    assert result["errors"] == []

    with SessionLocal() as db:
        row = db.scalar(select(EnvReading))
        assert row.sample_count == 58
        assert row.temp_avg == 23.41
        assert row.press_avg == 1013.25
        assert row.sensor_id == "default"
        assert row.gateway_id == "iroda-teszt"
        # Az időszak UTC-ben tárolódik.
        assert row.period_start_utc.isoformat() == "2026-09-01T06:00:00"


def test_reading_uuid_duplikacio_nem_hoz_letre_masodik_rekordot(gateway):
    reading = env_reading(START, END)

    first = gateway.env([reading]).json()
    assert first["accepted"] == [reading["reading_uuid"]]

    second = gateway.env([reading]).json()
    assert second["accepted"] == []
    assert second["duplicates"] == [reading["reading_uuid"]]
    assert second["errors"] == []

    assert _reading_count() == 1


def test_tartomanyon_kivuli_ertek_errorsba_kerul_a_jok_feldolgozodnak(gateway):
    jo = env_reading(START, END)
    tul_meleg = env_reading(START, END, temp_avg=120.0)
    negativ_para = env_reading(START, END, hum_avg=-5.0)
    rossz_nyomas = env_reading(START, END, press_avg=50.0)
    hideg_min = env_reading(START, END, temp_min=-80.0)
    jo_masodik = env_reading("2026-09-01T09:00:00+02:00", "2026-09-01T10:00:00+02:00")

    result = gateway.env(
        [jo, tul_meleg, negativ_para, rossz_nyomas, hideg_min, jo_masodik]
    ).json()

    assert set(result["accepted"]) == {jo["reading_uuid"], jo_masodik["reading_uuid"]}
    assert len(result["errors"]) == 4
    assert {error["reason"] for error in result["errors"]} == {"out_of_range"}
    assert {error["reading_uuid"] for error in result["errors"]} == {
        tul_meleg["reading_uuid"],
        negativ_para["reading_uuid"],
        rossz_nyomas["reading_uuid"],
        hideg_min["reading_uuid"],
    }
    assert _reading_count() == 2


def test_hatarertekek_meg_belefernek(gateway):
    hatareset = env_reading(START, END, temp_avg=-40.0, hum_avg=100.0, press_avg=300.0)
    result = gateway.env([hatareset]).json()
    assert result["accepted"] == [hatareset["reading_uuid"]]


def test_sample_count_legalabb_egy(gateway):
    nulla = env_reading(START, END, sample_count=0)
    result = gateway.env([nulla]).json()
    assert result["errors"] == [
        {"reading_uuid": nulla["reading_uuid"], "reason": "invalid_sample_count"}
    ]
    assert _reading_count() == 0


def test_hibas_idoszak_es_idobelyeg(gateway):
    forditott = env_reading(END, START)
    ertelmetlen = env_reading("tegnap", END)

    result = gateway.env([forditott, ertelmetlen]).json()
    reasons = {error["reading_uuid"]: error["reason"] for error in result["errors"]}
    assert reasons[forditott["reading_uuid"]] == "invalid_period"
    assert reasons[ertelmetlen["reading_uuid"]] == "invalid_timestamp"
    assert _reading_count() == 0


def test_hianyos_meres_invalid_payload(gateway):
    hianyos = {"reading_uuid": new_uuid(), "period_start": START}  # nincs period_end
    result = gateway.env([hianyos]).json()
    assert result["errors"] == [
        {"reading_uuid": hianyos["reading_uuid"], "reason": "invalid_payload"}
    ]


def test_tul_nagy_koteg_413(gateway):
    readings = [env_reading(START, END) for _ in range(MAX_READINGS_PER_BATCH + 1)]
    assert gateway.env(readings).status_code == 413
    assert _reading_count() == 0


def test_env_vegpont_api_kulcsot_igenyel(client):
    payload = {"gateway_id": "iroda-teszt", "readings": []}
    assert client.post("/api/v1/env", json=payload).status_code == 401


def test_riasztas_a_kuszobon_kivuli_atlagra(gateway):
    gateway.env([env_reading(START, END, temp_avg=31.0, hum_avg=41.0)])

    with SessionLocal() as db:
        latest = env_service.latest_reading(db)
        alerts = env_service.evaluate_alerts(latest)

    assert len(alerts) == 1
    assert "Meleg" in alerts[0]


def test_kuszobon_beluli_ertekre_nincs_riasztas(gateway):
    gateway.env([env_reading(START, END, temp_avg=22.0, hum_avg=45.0)])

    with SessionLocal() as db:
        assert env_service.evaluate_alerts(env_service.latest_reading(db)) == []


def test_env_series_vegpont(gateway, client):
    gateway.env(
        [
            env_reading(START, END, temp_avg=22.0),
            env_reading("2026-09-01T09:00:00+02:00", "2026-09-01T10:00:00+02:00", temp_avg=23.0),
        ]
    )

    response = client.get(
        "/api/v1/env/series?hours=720", headers={"X-API-Key": "teszt-api-kulcs"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["hours"] == 720
    assert [point["temp_avg"] for point in body["points"]] == [22.0, 23.0]
    # A grafikon lokális időt kap, hogy a tengely olvasható legyen.
    assert body["points"][0]["ts"].startswith("2026-09-01T08:00:00+02:00")


def test_env_series_hitelesitest_igenyel(client):
    assert client.get("/api/v1/env/series").status_code == 401
