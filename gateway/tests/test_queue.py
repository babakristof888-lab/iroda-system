"""A puffer életciklusa: beszúrás -> kiolvasás -> `sent` jelölés.

Ez a rendszer legfontosabb ígérete: ami a lemezre került, az nem veszhet el,
és nem is mehet fel kétszer feldolgozva.
"""

from __future__ import annotations

import time

import db


def _punch(conn, uuid_value: str, uid: str = "04A1B2C3", ts_epoch: float | None = None):
    epoch = time.time() if ts_epoch is None else ts_epoch
    db.insert_punch(conn, uuid_value, uid, f"ts-{uuid_value}", epoch, now_epoch=epoch)


def test_beszuras_utan_azonnal_pending(conn):
    _punch(conn, "u1")
    rows = db.pending_punches(conn, 10)
    assert [row["event_uuid"] for row in rows] == ["u1"]
    assert db.queue_size(conn) == 1


def test_a_legregebbi_megy_eloszor(conn):
    _punch(conn, "kesobbi", ts_epoch=2000.0)
    _punch(conn, "legregebbi", ts_epoch=1000.0)
    _punch(conn, "kozepso", ts_epoch=1500.0)
    rows = db.pending_punches(conn, 10)
    assert [row["event_uuid"] for row in rows] == ["legregebbi", "kozepso", "kesobbi"]


def test_batch_meret_korlatozza_a_kiolvasast(conn):
    for index in range(10):
        _punch(conn, f"u{index}", ts_epoch=1000.0 + index)
    assert len(db.pending_punches(conn, 4)) == 4


def test_sent_jeloles_utan_nem_jon_vissza(conn):
    _punch(conn, "u1")
    _punch(conn, "u2")
    db.mark_sent(conn, db.PUNCH_TABLE, ["u1"], "2026-09-06T10:00:00+02:00")

    rows = db.pending_punches(conn, 10)
    assert [row["event_uuid"] for row in rows] == ["u2"]
    assert db.queue_size(conn) == 1
    assert db.count_by_status(conn, db.PUNCH_TABLE, db.STATUS_SENT) == 1


def test_error_jeloles_megorzi_az_okot_es_nem_probalja_ujra(conn):
    _punch(conn, "u1")
    db.mark_error(conn, db.PUNCH_TABLE, [("u1", "invalid_timestamp")])

    assert db.pending_punches(conn, 10) == []
    row = conn.execute("SELECT status, last_error FROM punch_queue WHERE event_uuid='u1'").fetchone()
    assert row["status"] == db.STATUS_ERROR
    assert row["last_error"] == "invalid_timestamp"


def test_ugyanaz_az_event_uuid_nem_szurhato_be_ketszer(conn):
    import sqlite3

    _punch(conn, "u1")
    try:
        _punch(conn, "u1")
    except sqlite3.IntegrityError:
        pass
    else:  # pragma: no cover
        raise AssertionError("a UNIQUE megszorításnak meg kellett volna fognia")
    assert db.queue_size(conn) == 1


def test_kiserletszamlalo_no_de_a_sor_pending_marad(conn):
    _punch(conn, "u1")
    db.bump_attempts(conn, db.PUNCH_TABLE, ["u1"])
    db.bump_attempts(conn, db.PUNCH_TABLE, ["u1"])
    row = conn.execute("SELECT status, attempts FROM punch_queue WHERE event_uuid='u1'").fetchone()
    assert row["status"] == db.STATUS_PENDING
    assert row["attempts"] == 2


def test_queue_size_a_ket_sor_osszege(conn):
    _punch(conn, "u1")
    db.store_env_reading(
        conn,
        reading_uuid="r1",
        sensor_id="default",
        bucket=100,
        period_start="2026-09-06T08:00:00+02:00",
        period_end="2026-09-06T09:00:00+02:00",
        stats={
            "sample_count": 3,
            "temp_avg": 23.0, "temp_min": 22.0, "temp_max": 24.0,
            "hum_avg": 40.0, "hum_min": 39.0, "hum_max": 41.0,
            "press_avg": 1013.0,
        },
        created_at="2026-09-06T09:00:30+02:00",
    )
    assert db.queue_size(conn) == 2


def test_karbantartas_torli_a_regi_sent_sorokat_de_az_errort_soha(conn):
    now = 10_000_000.0
    old = now - 40 * 86400

    _punch(conn, "regi-sent", ts_epoch=old)
    _punch(conn, "regi-error", ts_epoch=old)
    _punch(conn, "friss-sent", ts_epoch=now)
    _punch(conn, "regi-pending", ts_epoch=old)

    db.mark_sent(conn, db.PUNCH_TABLE, ["regi-sent", "friss-sent"], "akkor")
    db.mark_error(conn, db.PUNCH_TABLE, [("regi-error", "invalid_payload")])

    removed = db.cleanup(conn, now, sent_retention_days=30, sample_retention_days=7)
    assert removed["punches"] == 1

    maradt = {
        row["event_uuid"]
        for row in conn.execute("SELECT event_uuid FROM punch_queue").fetchall()
    }
    assert maradt == {"regi-error", "friss-sent", "regi-pending"}


def test_karbantartas_torli_a_regi_feldolgozott_nyers_mintakat(conn):
    now = 10_000_000.0
    db.insert_env_sample(conn, "regi", now - 10 * 86400, 23.0, 40.0, 1013.0)
    db.insert_env_sample(conn, "regi-feldolgozatlan", now - 10 * 86400, 23.0, 40.0, 1013.0)
    db.insert_env_sample(conn, "friss", now - 3600, 23.0, 40.0, 1013.0)
    conn.execute("UPDATE env_samples SET aggregated = 1 WHERE ts_local = 'regi'")
    conn.commit()

    removed = db.cleanup(conn, now, sent_retention_days=30, sample_retention_days=7)
    assert removed["samples"] == 1

    maradt = {row["ts_local"] for row in conn.execute("SELECT ts_local FROM env_samples")}
    assert maradt == {"regi-feldolgozatlan", "friss"}


def test_meta_ertek_irhato_es_olvashato(conn):
    assert db.get_meta(conn, "nincs-ilyen") is None
    db.set_meta(conn, "kulcs", "1")
    db.set_meta(conn, "kulcs", "2")
    assert db.get_meta(conn, "kulcs") == "2"
