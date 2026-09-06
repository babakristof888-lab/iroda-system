"""A feltöltés: válaszfeldolgozás, backoff, hálózati hiba, prioritás.

Se hálózat, se szerver: a `requests.Session` helyén egy `FakeSession` áll.
"""

from __future__ import annotations

import time

import pytest
import requests

import db
import uploader
from conftest import FakeResponse, FakeSession
from status import Status


# --------------------------------------------------------------------------
# Backoff
# --------------------------------------------------------------------------
def test_backoff_5_10_20_40_majd_max_300():
    assert uploader.backoff_delay(0) == 5
    assert uploader.backoff_delay(1) == 5
    assert uploader.backoff_delay(2) == 10
    assert uploader.backoff_delay(3) == 20
    assert uploader.backoff_delay(4) == 40
    assert uploader.backoff_delay(5) == 80
    assert uploader.backoff_delay(6) == 160
    assert uploader.backoff_delay(7) == 300
    assert uploader.backoff_delay(8) == 300
    assert uploader.backoff_delay(50) == 300


def test_backoff_soha_nem_no_300_fole():
    assert all(uploader.backoff_delay(n) <= 300 for n in range(0, 100))


# --------------------------------------------------------------------------
# Válaszfeldolgozás
# --------------------------------------------------------------------------
def test_a_duplicates_is_sikeres_feldolgozas():
    """A duplikátum azt jelenti, hogy a szerver már megkapta, csak a válasz
    veszett el. Újraküldeni értelmetlen lenne."""
    body = {"accepted": ["a"], "duplicates": ["b"], "errors": []}
    sent, failures = uploader.classify_response(body, "event_uuid", ["a", "b"])
    assert sorted(sent) == ["a", "b"]
    assert failures == []


def test_errors_okkal_egyutt():
    body = {
        "accepted": ["a"],
        "duplicates": [],
        "errors": [{"event_uuid": "b", "reason": "invalid_timestamp"}],
    }
    sent, failures = uploader.classify_response(body, "event_uuid", ["a", "b"])
    assert sent == ["a"]
    assert failures == [("b", "invalid_timestamp")]


def test_a_valaszban_nem_emlitett_uuid_pendingben_marad():
    body = {"accepted": ["a"], "duplicates": [], "errors": []}
    sent, failures = uploader.classify_response(body, "event_uuid", ["a", "b"])
    assert sent == ["a"]
    assert failures == []


def test_idegen_uuid_a_valaszban_nem_zavar():
    body = {"accepted": ["a", "sosem-kuldtuk"], "duplicates": [], "errors": []}
    sent, _ = uploader.classify_response(body, "event_uuid", ["a"])
    assert sent == ["a"]


@pytest.mark.parametrize("body", [None, [], "hopp", {"accepted": "nem-lista"}, {}])
def test_ertelmetlen_valasz_nem_dob_kivetelt(body):
    sent, failures = uploader.classify_response(body, "event_uuid", ["a"])
    assert sent == []
    assert failures == []


def test_env_valasz_reading_uuid_kulccsal():
    body = {
        "accepted": ["r1"],
        "duplicates": ["r2"],
        "errors": [{"reading_uuid": "r3", "reason": "out_of_range"}],
    }
    sent, failures = uploader.classify_response(body, "reading_uuid", ["r1", "r2", "r3"])
    assert sorted(sent) == ["r1", "r2"]
    assert failures == [("r3", "out_of_range")]


# --------------------------------------------------------------------------
# Teljes feltöltési kör
# --------------------------------------------------------------------------
def _uploader(cfg, conn, session):
    return uploader.Uploader(cfg, Status(serial_ok=True, bme_ok=True), conn, session=session)


def _punch(conn, uuid_value, ts_epoch=None):
    epoch = time.time() if ts_epoch is None else ts_epoch
    db.insert_punch(conn, uuid_value, "04A1B2C3", f"ts-{uuid_value}", epoch, now_epoch=epoch)


def _reading(conn, uuid_value, bucket):
    db.store_env_reading(
        conn,
        reading_uuid=uuid_value,
        sensor_id="default",
        bucket=bucket,
        period_start="2026-09-06T08:00:00+02:00",
        period_end="2026-09-06T09:00:00+02:00",
        stats={
            "sample_count": 5,
            "temp_avg": 23.0, "temp_min": 22.0, "temp_max": 24.0,
            "hum_avg": 40.0, "hum_min": 39.0, "hum_max": 41.0,
            "press_avg": 1013.0,
        },
        created_at="2026-09-06T09:00:30+02:00",
    )


def test_sikeres_feltoltes_utan_sent_lesz(cfg, conn):
    _punch(conn, "u1")
    _punch(conn, "u2")
    session = FakeSession([FakeResponse(200, {"accepted": ["u1", "u2"], "duplicates": [], "errors": []})])

    result = _uploader(cfg, conn, session).upload_punches()
    assert result.outcome == uploader.OK
    assert result.sent == 2
    assert db.pending_punches(conn, 10) == []

    call = session.calls[0]
    assert call["url"] == cfg.events_url
    assert call["headers"]["X-API-Key"] == "teszt-kulcs"
    assert call["json"]["gateway_id"] == "iroda-teszt"
    assert [event["event_uuid"] for event in call["json"]["events"]] == ["u1", "u2"]
    assert call["timeout"] == (5, 15)


def test_duplicates_valasz_eseten_sent_lesz_nem_probaljuk_ujra(cfg, conn):
    _punch(conn, "u1")
    session = FakeSession([FakeResponse(200, {"accepted": [], "duplicates": ["u1"], "errors": []})])

    up = _uploader(cfg, conn, session)
    assert up.upload_punches().outcome == uploader.OK
    assert db.pending_punches(conn, 10) == []

    row = conn.execute("SELECT status FROM punch_queue WHERE event_uuid='u1'").fetchone()
    assert row["status"] == db.STATUS_SENT

    # A következő körben nincs mit küldeni: nem megy újabb HTTP kérés.
    assert up.upload_punches().outcome == uploader.IDLE
    assert len(session.calls) == 1


def test_halozati_hiba_utan_minden_pendingben_marad(cfg, conn):
    _punch(conn, "u1")
    _punch(conn, "u2")
    session = FakeSession([requests.ConnectionError("nincs hálózat")])

    result = _uploader(cfg, conn, session).upload_punches()
    assert result.outcome == uploader.FAIL
    assert [row["event_uuid"] for row in db.pending_punches(conn, 10)] == ["u1", "u2"]
    assert conn.execute(
        "SELECT attempts FROM punch_queue WHERE event_uuid='u1'"
    ).fetchone()["attempts"] == 1


def test_timeout_utan_is_pendingben_marad(cfg, conn):
    _punch(conn, "u1")
    session = FakeSession([requests.Timeout("lejárt")])
    assert _uploader(cfg, conn, session).upload_punches().outcome == uploader.FAIL
    assert len(db.pending_punches(conn, 10)) == 1


@pytest.mark.parametrize("status_code", [500, 502, 503, 504])
def test_szerverhiba_utan_pendingben_marad(cfg, conn, status_code):
    _punch(conn, "u1")
    session = FakeSession([FakeResponse(status_code, None, "hiba")])
    assert _uploader(cfg, conn, session).upload_punches().outcome == uploader.FAIL
    assert len(db.pending_punches(conn, 10)) == 1


def test_rossz_api_kulcs_eseten_sem_veszik_el_semmi(cfg, conn):
    """401: a kulcs rossz. A sorok maradnak, hogy a javítás után felmenjenek."""
    _punch(conn, "u1")
    session = FakeSession([FakeResponse(401, None, "unauthorized")])
    assert _uploader(cfg, conn, session).upload_punches().outcome == uploader.FAIL
    assert len(db.pending_punches(conn, 10)) == 1


def test_ertelmetlen_json_valasz_eseten_is_pendingben_marad(cfg, conn):
    _punch(conn, "u1")
    session = FakeSession([FakeResponse(200, None, "<html>proxy</html>")])
    assert _uploader(cfg, conn, session).upload_punches().outcome == uploader.FAIL
    assert len(db.pending_punches(conn, 10)) == 1


def test_a_szerver_altal_visszautasitott_sor_error_lesz_es_nem_jon_vissza(cfg, conn):
    _punch(conn, "jo")
    _punch(conn, "rossz")
    session = FakeSession([
        FakeResponse(200, {
            "accepted": ["jo"],
            "duplicates": [],
            "errors": [{"event_uuid": "rossz", "reason": "invalid_timestamp"}],
        })
    ])
    up = _uploader(cfg, conn, session)
    result = up.upload_punches()
    assert result.sent == 1
    assert result.errors == 1
    assert db.pending_punches(conn, 10) == []
    assert up.upload_punches().outcome == uploader.IDLE


def test_legfeljebb_100_belyegzes_megy_egy_kotegben(cfg, conn):
    for index in range(150):
        _punch(conn, f"u{index:03d}", ts_epoch=1000.0 + index)
    session = FakeSession([FakeResponse(200, {"accepted": [], "duplicates": [], "errors": []})])
    _uploader(cfg, conn, session).upload_punches()
    assert len(session.calls[0]["json"]["events"]) == 100


def test_legfeljebb_50_meres_megy_egy_kotegben(cfg, conn):
    for index in range(80):
        _reading(conn, f"r{index:03d}", bucket=1000 + index)
    session = FakeSession([FakeResponse(200, {"accepted": [], "duplicates": [], "errors": []})])
    _uploader(cfg, conn, session).upload_env()
    assert len(session.calls[0]["json"]["readings"]) == 50


def test_env_koteg_mezoi_a_szerzodes_szerint(cfg, conn):
    _reading(conn, "r1", bucket=1000)
    session = FakeSession([FakeResponse(200, {"accepted": ["r1"], "duplicates": [], "errors": []})])
    _uploader(cfg, conn, session).upload_env()

    call = session.calls[0]
    assert call["url"] == cfg.env_url
    reading = call["json"]["readings"][0]
    assert set(reading) == {
        "reading_uuid", "sensor_id", "period_start", "period_end", "sample_count",
        "temp_avg", "temp_min", "temp_max", "hum_avg", "hum_min", "hum_max", "press_avg",
    }
    assert reading["sample_count"] == 5


def test_a_belyegzes_megy_eloszor_es_halozati_hibanal_az_env_meg_sem_indul(cfg, conn):
    _punch(conn, "u1")
    _reading(conn, "r1", bucket=1000)
    session = FakeSession([requests.ConnectionError("nincs hálózat")])

    assert _uploader(cfg, conn, session).upload_once() == uploader.FAIL
    assert len(session.calls) == 1
    assert session.calls[0]["url"] == cfg.events_url


def test_sikeres_belyegzes_utan_az_env_is_megy(cfg, conn):
    _punch(conn, "u1")
    _reading(conn, "r1", bucket=1000)
    session = FakeSession([
        FakeResponse(200, {"accepted": ["u1"], "duplicates": [], "errors": []}),
        FakeResponse(200, {"accepted": ["r1"], "duplicates": [], "errors": []}),
    ])
    assert _uploader(cfg, conn, session).upload_once() == uploader.OK
    assert [call["url"] for call in session.calls] == [cfg.events_url, cfg.env_url]
    assert db.queue_size(conn) == 0


def test_ures_puffernel_nincs_halozati_forgalom(cfg, conn):
    session = FakeSession()
    assert _uploader(cfg, conn, session).upload_once() == uploader.IDLE
    assert session.calls == []


# --------------------------------------------------------------------------
# Heartbeat
# --------------------------------------------------------------------------
def test_heartbeat_tartalma(cfg, conn):
    _punch(conn, "u1")
    _reading(conn, "r1", bucket=1000)
    session = FakeSession([FakeResponse(200, {"ok": True, "server_time": "..."})])

    up = uploader.Uploader(cfg, Status(serial_ok=False, bme_ok=True), conn, session=session)
    assert up.heartbeat() is True

    payload = session.calls[0]["json"]
    assert session.calls[0]["url"] == cfg.heartbeat_url
    assert payload["gateway_id"] == "iroda-teszt"
    assert payload["version"] == cfg.version
    assert payload["queue_size"] == 2      # a két sor pending elemeinek összege
    assert payload["serial_ok"] is False
    assert payload["bme_ok"] is True


def test_heartbeat_halozati_hiba_eseten_nem_dob_kivetelt(cfg, conn):
    session = FakeSession([requests.ConnectionError("nincs hálózat")])
    assert _uploader(cfg, conn, session).heartbeat() is False


# --------------------------------------------------------------------------
# Karbantartás
# --------------------------------------------------------------------------
def test_karbantartas_naponta_egyszer_esedekes(cfg, conn):
    up = _uploader(cfg, conn, FakeSession())
    now = 10_000_000.0

    assert up.maintenance_due(now) is True
    up.maintenance(now)
    assert up.maintenance_due(now + 3600) is False
    assert up.maintenance_due(now + 25 * 3600) is True
