"""Az órás összesítés.

Itt dől el, hogy megmarad-e a délutáni csúcs: az átlag mellett a minimumot és
a maximumot is elküldjük, mert egy óránkénti pillanatfelvételből semmi nem
látszana.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import aggregator
import db

BUDAPEST = ZoneInfo("Europe/Budapest")


def _epoch(text: str) -> float:
    return datetime.fromisoformat(text).timestamp()


def _sample(conn, ts_text: str, temp, hum, press):
    epoch = _epoch(ts_text)
    db.insert_env_sample(conn, ts_text, epoch, temp, hum, press)


def test_atlag_minimum_maximum():
    rows = [
        {"temp": 22.0, "hum": 40.0, "press": 1010.0},
        {"temp": 26.0, "hum": 44.0, "press": 1012.0},
        {"temp": 24.0, "hum": 42.0, "press": 1011.0},
    ]
    stats = aggregator.aggregate(rows)
    assert stats["sample_count"] == 3
    assert stats["temp_avg"] == 24.0
    assert stats["temp_min"] == 22.0
    assert stats["temp_max"] == 26.0
    assert stats["hum_avg"] == 42.0
    assert stats["hum_min"] == 40.0
    assert stats["hum_max"] == 44.0
    assert stats["press_avg"] == 1011.0


def test_a_csucsertek_akkor_is_megmarad_ha_az_atlag_alacsony():
    """Ez a lényeg: 40 fokos csúcs egy 26 fokos átlagú órában."""
    rows = [{"temp": 24.0, "hum": 40.0, "press": 1010.0} for _ in range(58)]
    rows.append({"temp": 40.2, "hum": 40.0, "press": 1010.0})
    rows.append({"temp": 39.8, "hum": 40.0, "press": 1010.0})

    stats = aggregator.aggregate(rows)
    assert stats["temp_max"] == 40.2
    assert stats["temp_avg"] < 25.0


def test_egyetlen_minta():
    stats = aggregator.aggregate([{"temp": 23.5, "hum": 41.0, "press": 1013.0}])
    assert stats["sample_count"] == 1
    assert stats["temp_avg"] == stats["temp_min"] == stats["temp_max"] == 23.5


def test_ket_tizedesre_kerekit():
    rows = [{"temp": 23.333, "hum": 41.0, "press": 1013.0},
            {"temp": 23.334, "hum": 41.0, "press": 1013.0}]
    assert aggregator.aggregate(rows)["temp_avg"] == 23.33


def test_ures_lista():
    stats = aggregator.aggregate([])
    assert stats["sample_count"] == 0
    assert stats["temp_avg"] is None
    assert stats["temp_min"] is None


def test_savhatarok_lokalis_idoben_offszettel():
    bucket = db.bucket_of(_epoch("2026-09-06T08:30:00+02:00"))
    start, end = aggregator.bucket_bounds(bucket, BUDAPEST)
    assert start == "2026-09-06T08:00:00+02:00"
    assert end == "2026-09-06T09:00:00+02:00"


def test_savhatarok_teli_idoszamitasban():
    bucket = db.bucket_of(_epoch("2026-01-15T08:30:00+01:00"))
    start, end = aggregator.bucket_bounds(bucket, BUDAPEST)
    assert start == "2026-01-15T08:00:00+01:00"
    assert end == "2026-01-15T09:00:00+01:00"


def test_lezart_ora_osszesitese(conn, cfg):
    _sample(conn, "2026-09-06T08:05:00+02:00", 22.0, 40.0, 1010.0)
    _sample(conn, "2026-09-06T08:35:00+02:00", 26.0, 44.0, 1012.0)

    created = aggregator.run_pending_aggregations(
        conn, cfg, now_epoch=_epoch("2026-09-06T09:00:30+02:00")
    )
    assert created == 1

    rows = db.pending_env_readings(conn, 10)
    assert len(rows) == 1
    row = rows[0]
    assert row["period_start"] == "2026-09-06T08:00:00+02:00"
    assert row["period_end"] == "2026-09-06T09:00:00+02:00"
    assert row["sample_count"] == 2
    assert row["temp_avg"] == 24.0
    assert row["temp_min"] == 22.0
    assert row["temp_max"] == 26.0
    assert row["sensor_id"] == "default"


def test_a_folyamatban_levo_ora_meg_nem_kerul_sorra(conn, cfg):
    _sample(conn, "2026-09-06T09:05:00+02:00", 22.0, 40.0, 1010.0)
    created = aggregator.run_pending_aggregations(
        conn, cfg, now_epoch=_epoch("2026-09-06T09:30:00+02:00")
    )
    assert created == 0
    assert db.pending_env_readings(conn, 10) == []


def test_ures_orara_nem_keletkezik_sor(conn, cfg):
    """A szenzor offline volt: az az óra egyszerűen kimarad, nem lesz üres sor.
    A szerver ebből fogja tudni, hogy hiány volt."""
    _sample(conn, "2026-09-06T08:05:00+02:00", 22.0, 40.0, 1010.0)
    # 09:00-10:00 között nincs egyetlen minta sem
    _sample(conn, "2026-09-06T10:05:00+02:00", 23.0, 41.0, 1011.0)

    created = aggregator.run_pending_aggregations(
        conn, cfg, now_epoch=_epoch("2026-09-06T11:00:30+02:00")
    )
    assert created == 2

    starts = [row["period_start"] for row in db.pending_env_readings(conn, 10)]
    assert starts == ["2026-09-06T08:00:00+02:00", "2026-09-06T10:00:00+02:00"]


def test_tobb_kimaradt_ora_visszamenoleges_feldolgozasa(conn, cfg):
    """A gateway három napig állt: induláskor minden lezárt óra feldolgozódik."""
    for hour in range(8, 14):
        _sample(conn, f"2026-09-06T{hour:02d}:10:00+02:00", 20.0 + hour, 40.0, 1010.0)
        _sample(conn, f"2026-09-06T{hour:02d}:40:00+02:00", 22.0 + hour, 42.0, 1012.0)

    created = aggregator.run_pending_aggregations(
        conn, cfg, now_epoch=_epoch("2026-09-06T14:00:30+02:00")
    )
    assert created == 6

    rows = db.pending_env_readings(conn, 50)
    assert len(rows) == 6
    assert [row["period_start"] for row in rows] == [
        f"2026-09-06T{hour:02d}:00:00+02:00" for hour in range(8, 14)
    ]
    assert rows[0]["temp_avg"] == 29.0   # (28 + 30) / 2
    assert rows[0]["sample_count"] == 2


def test_a_feldolgozott_mintak_meg_nem_torlodnek_csak_megjelolodnek(conn, cfg):
    _sample(conn, "2026-09-06T08:05:00+02:00", 22.0, 40.0, 1010.0)
    aggregator.run_pending_aggregations(conn, cfg, now_epoch=_epoch("2026-09-06T09:00:30+02:00"))

    row = conn.execute("SELECT COUNT(*) AS n, SUM(aggregated) AS a FROM env_samples").fetchone()
    assert row["n"] == 1
    assert row["a"] == 1


def test_ketszeri_futas_nem_hoz_letre_ket_sort(conn, cfg):
    _sample(conn, "2026-09-06T08:05:00+02:00", 22.0, 40.0, 1010.0)
    now = _epoch("2026-09-06T09:00:30+02:00")
    assert aggregator.run_pending_aggregations(conn, cfg, now_epoch=now) == 1
    assert aggregator.run_pending_aggregations(conn, cfg, now_epoch=now) == 0
    assert len(db.pending_env_readings(conn, 10)) == 1


def test_kesve_erkezo_minta_nem_hoz_letre_masodik_sort_ugyanarra_az_orara(conn, cfg):
    """Ha egy már összesített órához utólag jön minta, nem keletkezik második
    sor ugyanarra az órára – a szerver `sensor_id` + időszak szerint egy sort vár."""
    _sample(conn, "2026-09-06T08:05:00+02:00", 22.0, 40.0, 1010.0)
    now = _epoch("2026-09-06T09:00:30+02:00")
    aggregator.run_pending_aggregations(conn, cfg, now_epoch=now)

    _sample(conn, "2026-09-06T08:50:00+02:00", 26.0, 44.0, 1012.0)
    aggregator.run_pending_aggregations(conn, cfg, now_epoch=now)

    assert len(db.pending_env_readings(conn, 10)) == 1
    # A késve érkezett minta is feldolgozottnak számít, nem próbáljuk örökké.
    assert conn.execute("SELECT SUM(aggregated) AS a FROM env_samples").fetchone()["a"] == 2
