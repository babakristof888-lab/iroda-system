"""Órás statisztikai összesítés a környezeti mintákból.

**Miért a gateway csinálja és nem az Arduino:** ha a Nano döntene óránként, egy
újraindulás elmosná a számlálót, és óránként egyetlen mintából nem látszana
semmi. Ha délután 40 fok volt a szerverszobában, de a 15:00-s pillanatfelvétel
épp 26-ot fogott, az sosem derülne ki. A gépen van valódi óra és van puffer,
így a csúcsértékek is megmaradnak.

Ez az egyetlen hely a gateway-ben, ahol az adatot nem csak továbbítjuk.
Küszöböt, riasztást, értékelést itt sem végzünk – az a szerver dolga.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Sequence
from zoneinfo import ZoneInfo

import db
from config import SENSOR_ID, Config

log = logging.getLogger("gateway.aggregator")

# Ilyen sűrűn nézzük meg, hogy lezárult-e egy óra. (Egy óra letelte után
# legkésőbb ennyivel készül el az összesítő.)
CHECK_INTERVAL_SECONDS = 30.0


def _mean(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def _numbers(rows: Sequence[Any], key: str) -> list[float]:
    result: list[float] = []
    for row in rows:
        value = row[key]
        if value is None:
            continue
        try:
            result.append(float(value))
        except (TypeError, ValueError):
            continue
    return result


def aggregate(rows: Sequence[Any]) -> dict[str, Any]:
    """Egy óra mintáiból a szervernek küldendő statisztika.

    Tiszta függvény, adatbázis nélkül – ezért közvetlenül tesztelhető.
    """
    temps = _numbers(rows, "temp")
    hums = _numbers(rows, "hum")
    presses = _numbers(rows, "press")
    return {
        "sample_count": len(rows),
        "temp_avg": _mean(temps),
        "temp_min": round(min(temps), 2) if temps else None,
        "temp_max": round(max(temps), 2) if temps else None,
        "hum_avg": _mean(hums),
        "hum_min": round(min(hums), 2) if hums else None,
        "hum_max": round(max(hums), 2) if hums else None,
        "press_avg": _mean(presses),
    }


def bucket_bounds(bucket: int, tz: ZoneInfo) -> tuple[str, str]:
    """Egy órasáv kezdete és vége lokális ISO-8601 alakban, offszettel.

    A szerver ebből csinál UTC-t, tehát az offszetnek benne kell lennie.
    """
    start = datetime.fromtimestamp(bucket * 3600, tz=tz)
    end = start + timedelta(hours=1)
    return start.isoformat(), end.isoformat()


def run_pending_aggregations(
    conn: sqlite3.Connection, cfg: Config, now_epoch: float | None = None
) -> int:
    """Minden befejezett, még össze nem sített órát feldolgoz.

    Induláskor ez dolgozza fel visszamenőleg a kimaradt órákat is: nem az idő
    múlását figyeljük, hanem azt, hogy melyik lezárt órához van még feldolgozatlan
    minta. Ha egy órában nulla minta volt (szenzor offline), nincs is `bucket`ja,
    így üres sor sem keletkezik – a szerver ebből fogja tudni, hogy hiány volt.
    """
    now = time.time() if now_epoch is None else now_epoch
    buckets = db.closed_buckets(conn, now)
    created = 0

    for bucket in buckets:
        rows = db.samples_in_bucket(conn, bucket)
        if not rows:
            continue
        stats = aggregate(rows)
        period_start, period_end = bucket_bounds(bucket, cfg.tz)
        created_at = datetime.now(cfg.tz).isoformat()
        stored = db.store_env_reading(
            conn,
            reading_uuid=str(uuid.uuid4()),
            sensor_id=SENSOR_ID,
            bucket=bucket,
            period_start=period_start,
            period_end=period_end,
            stats=stats,
            created_at=created_at,
        )
        if stored:
            created += 1
            log.info(
                "Órás összesítés %s – %s: %d minta, hőm. átl/min/max %s/%s/%s °C, "
                "pára átl %s %%, nyomás átl %s hPa",
                period_start,
                period_end,
                stats["sample_count"],
                stats["temp_avg"],
                stats["temp_min"],
                stats["temp_max"],
                stats["hum_avg"],
                stats["press_avg"],
            )
        else:
            log.warning(
                "A(z) %s órához már volt összesítő sor, csak a nyers mintákat jelöltem meg",
                period_start,
            )
    return created


def run(cfg: Config, stop: threading.Event) -> None:
    """Az EnvAggregator szál törzse."""
    conn = db.connect(cfg.db_path)
    try:
        db.init_schema(conn)
        log.info("EnvAggregator elindult (ellenőrzés %.0f másodpercenként)", CHECK_INTERVAL_SECONDS)
        while not stop.is_set():
            try:
                run_pending_aggregations(conn, cfg)
            except sqlite3.Error:
                log.exception("Az órás összesítés adatbázishiba miatt kimaradt")
            stop.wait(CHECK_INTERVAL_SECONDS)
    finally:
        conn.close()
