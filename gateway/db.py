"""A lemezre írt puffer: egyetlen SQLite fájl (`queue.db`).

Három tábla:

* `punch_queue`  – a felolvasott kártyák, feltöltésre várva,
* `env_samples`  – a percenkénti nyers szenzorminták,
* `env_queue`    – a percenkénti mintákból képzett órás összesítők.

A `sqlite3` modulon kívül semmit nem használunk. Minden szál **saját
kapcsolatot** nyit (`connect()`), így nincs szükség zárolásra a Python
oldalán; a WAL napló miatt az olvasás és az írás sem akad egymásra.

`synchronous=FULL`: a bélyegzés a lemezen van, mielőtt a `commit` visszatér.
Ez a rendszer egyik alapígérete – ha a gép abban a pillanatban áramtalanodik,
a bélyegzés akkor is megmarad. A pár írás/perc mellett ennek nincs ára.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

PUNCH_TABLE = "punch_queue"
ENV_TABLE = "env_queue"

STATUS_PENDING = "pending"
STATUS_SENT = "sent"
STATUS_ERROR = "error"

# Melyik táblában hogy hívják a szerver felé küldött azonosítót.
UUID_COLUMN = {PUNCH_TABLE: "event_uuid", ENV_TABLE: "reading_uuid"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS punch_queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_uuid    TEXT NOT NULL UNIQUE,
    uid           TEXT NOT NULL,
    ts_local      TEXT NOT NULL,
    ts_epoch      REAL NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    created_at    TEXT NOT NULL,
    created_epoch REAL NOT NULL,
    sent_at       TEXT
);
CREATE INDEX IF NOT EXISTS ix_punch_queue_status ON punch_queue(status, ts_epoch, id);

CREATE TABLE IF NOT EXISTS env_samples (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_local   TEXT NOT NULL,
    ts_epoch   REAL NOT NULL,
    bucket     INTEGER NOT NULL,
    temp       REAL,
    hum        REAL,
    press      REAL,
    aggregated INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_env_samples_bucket ON env_samples(aggregated, bucket);

CREATE TABLE IF NOT EXISTS env_queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    reading_uuid  TEXT NOT NULL UNIQUE,
    sensor_id     TEXT NOT NULL DEFAULT 'default',
    bucket        INTEGER NOT NULL UNIQUE,
    period_start  TEXT NOT NULL,
    period_end    TEXT NOT NULL,
    period_epoch  REAL NOT NULL,
    sample_count  INTEGER NOT NULL,
    temp_avg      REAL,
    temp_min      REAL,
    temp_max      REAL,
    hum_avg       REAL,
    hum_min       REAL,
    hum_max       REAL,
    press_avg     REAL,
    status        TEXT NOT NULL DEFAULT 'pending',
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    created_at    TEXT NOT NULL,
    created_epoch REAL NOT NULL,
    sent_at       TEXT
);
CREATE INDEX IF NOT EXISTS ix_env_queue_status ON env_queue(status, period_epoch, id);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Kapcsolat nyitása. Szálanként egyet kell nyitni."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript(SCHEMA)


# --------------------------------------------------------------------------
# Beszúrás – a SerialReader szál használja
# --------------------------------------------------------------------------
def insert_punch(
    conn: sqlite3.Connection,
    event_uuid: str,
    uid: str,
    ts_local: str,
    ts_epoch: float,
    now_epoch: float | None = None,
) -> None:
    """Egy bélyegzés a pufferbe, azonnali commit-tal."""
    now = time.time() if now_epoch is None else now_epoch
    with conn:
        conn.execute(
            "INSERT INTO punch_queue "
            "(event_uuid, uid, ts_local, ts_epoch, status, created_at, created_epoch) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event_uuid, uid, ts_local, ts_epoch, STATUS_PENDING, ts_local, now),
        )


def insert_env_sample(
    conn: sqlite3.Connection,
    ts_local: str,
    ts_epoch: float,
    temp: float | None,
    hum: float | None,
    press: float | None,
) -> None:
    """Egy nyers környezeti minta, azonnali commit-tal."""
    with conn:
        conn.execute(
            "INSERT INTO env_samples (ts_local, ts_epoch, bucket, temp, hum, press) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ts_local, ts_epoch, bucket_of(ts_epoch), temp, hum, press),
        )


# --------------------------------------------------------------------------
# Órás sávok – az EnvAggregator szál használja
# --------------------------------------------------------------------------
def bucket_of(epoch: float) -> int:
    """Melyik egész órába esik ez a pillanat (UTC óra-index).

    Miért UTC: Budapesten az UTC-hez képesti eltolás mindig egész óra, ezért az
    UTC órahatár egyben lokális órahatár is – viszont az óraátállítás napján
    nem fordul elő ismétlődő vagy hiányzó lokális óra. A sáv határait már
    lokális időben, offszettel írjuk ki a szervernek.
    """
    return int(epoch // 3600)


def closed_buckets(conn: sqlite3.Connection, now_epoch: float) -> list[int]:
    """A már befejezett, még össze nem sített órák, időrendben.

    Ha a gateway napokig állt, itt egyszerre jön vissza az összes kimaradt óra.
    """
    current = bucket_of(now_epoch)
    rows = conn.execute(
        "SELECT DISTINCT bucket FROM env_samples "
        "WHERE aggregated = 0 AND bucket < ? ORDER BY bucket",
        (current,),
    ).fetchall()
    return [int(row["bucket"]) for row in rows]


def samples_in_bucket(conn: sqlite3.Connection, bucket: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT temp, hum, press FROM env_samples "
        "WHERE bucket = ? AND aggregated = 0 ORDER BY ts_epoch",
        (bucket,),
    ).fetchall()


def store_env_reading(
    conn: sqlite3.Connection,
    reading_uuid: str,
    sensor_id: str,
    bucket: int,
    period_start: str,
    period_end: str,
    stats: dict[str, Any],
    created_at: str,
    now_epoch: float | None = None,
) -> bool:
    """Az órás összesítő a feltöltési sorba, és a nyers minták megjelölése.

    A kettő **egy tranzakcióban** történik: nem fordulhat elő, hogy a minták
    feldolgozottnak látszanak, de az összesítő sor nincs meg.

    `False`, ha erre az órára már van sor (pl. a gateway kétszer indult el
    ugyanabban a percben) – ilyenkor csak a mintákat jelöljük meg.
    """
    now = time.time() if now_epoch is None else now_epoch
    with conn:
        exists = conn.execute("SELECT 1 FROM env_queue WHERE bucket = ?", (bucket,)).fetchone()
        if exists is None:
            conn.execute(
                "INSERT INTO env_queue (reading_uuid, sensor_id, bucket, period_start, "
                "period_end, period_epoch, sample_count, temp_avg, temp_min, temp_max, "
                "hum_avg, hum_min, hum_max, press_avg, status, created_at, created_epoch) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    reading_uuid,
                    sensor_id,
                    bucket,
                    period_start,
                    period_end,
                    float(bucket) * 3600.0,
                    stats["sample_count"],
                    stats["temp_avg"],
                    stats["temp_min"],
                    stats["temp_max"],
                    stats["hum_avg"],
                    stats["hum_min"],
                    stats["hum_max"],
                    stats["press_avg"],
                    STATUS_PENDING,
                    created_at,
                    now,
                ),
            )
        conn.execute(
            "UPDATE env_samples SET aggregated = 1 WHERE bucket = ? AND aggregated = 0",
            (bucket,),
        )
    return exists is None


# --------------------------------------------------------------------------
# Feltöltés – az Uploader szál használja
# --------------------------------------------------------------------------
def pending_punches(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """A legrégebbi feltöltésre váró bélyegzések."""
    return conn.execute(
        "SELECT id, event_uuid, uid, ts_local FROM punch_queue "
        "WHERE status = ? ORDER BY ts_epoch, id LIMIT ?",
        (STATUS_PENDING, limit),
    ).fetchall()


def pending_env_readings(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, reading_uuid, sensor_id, period_start, period_end, sample_count, "
        "temp_avg, temp_min, temp_max, hum_avg, hum_min, hum_max, press_avg "
        "FROM env_queue WHERE status = ? ORDER BY period_epoch, id LIMIT ?",
        (STATUS_PENDING, limit),
    ).fetchall()


def mark_sent(
    conn: sqlite3.Connection,
    table: str,
    uuids: Sequence[str],
    sent_at: str,
) -> int:
    """Sikeresen feldolgozott sorok (`accepted` ÉS `duplicates`)."""
    if not uuids:
        return 0
    column = UUID_COLUMN[table]
    with conn:
        cursor = conn.executemany(
            f"UPDATE {table} SET status = ?, sent_at = ?, last_error = NULL "  # noqa: S608
            f"WHERE {column} = ? AND status = ?",
            [(STATUS_SENT, sent_at, value, STATUS_PENDING) for value in uuids],
        )
    return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else len(uuids)


def mark_error(
    conn: sqlite3.Connection,
    table: str,
    failures: Iterable[tuple[str, str]],
) -> int:
    """A szerver által visszautasított sorok.

    Ezeket soha nem próbáljuk újra: a `reason` (pl. `invalid_timestamp`) a
    következő kísérletnél is ugyanaz lenne. A sor megmarad, hogy utólag
    látszódjon, mi történt.
    """
    items = list(failures)
    if not items:
        return 0
    column = UUID_COLUMN[table]
    with conn:
        conn.executemany(
            f"UPDATE {table} SET status = ?, last_error = ? WHERE {column} = ?",  # noqa: S608
            [(STATUS_ERROR, reason, value) for value, reason in items],
        )
    return len(items)


def bump_attempts(conn: sqlite3.Connection, table: str, uuids: Sequence[str]) -> None:
    """Sikertelen kísérlet: a sorok `pending`-ben maradnak, csak a számláló nő."""
    if not uuids:
        return
    column = UUID_COLUMN[table]
    with conn:
        conn.executemany(
            f"UPDATE {table} SET attempts = attempts + 1 WHERE {column} = ?",  # noqa: S608
            [(value,) for value in uuids],
        )


def count_by_status(conn: sqlite3.Connection, table: str, status: str) -> int:
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM {table} WHERE status = ?", (status,)  # noqa: S608
    ).fetchone()
    return int(row["n"])


def queue_size(conn: sqlite3.Connection) -> int:
    """A heartbeatbe kerülő érték: a két sor pending elemeinek összege."""
    return count_by_status(conn, PUNCH_TABLE, STATUS_PENDING) + count_by_status(
        conn, ENV_TABLE, STATUS_PENDING
    )


# --------------------------------------------------------------------------
# Karbantartás
# --------------------------------------------------------------------------
def cleanup(
    conn: sqlite3.Connection,
    now_epoch: float,
    sent_retention_days: int,
    sample_retention_days: int,
) -> dict[str, int]:
    """Napi takarítás.

    Az `error` státuszú sorokat **soha** nem törli: azok a bizonyítékok arról,
    hogy a szerver mit utasított vissza.
    """
    sent_before = now_epoch - sent_retention_days * 86400
    samples_before = now_epoch - sample_retention_days * 86400

    with conn:
        punches = conn.execute(
            "DELETE FROM punch_queue WHERE status = ? AND created_epoch < ?",
            (STATUS_SENT, sent_before),
        ).rowcount
        readings = conn.execute(
            "DELETE FROM env_queue WHERE status = ? AND created_epoch < ?",
            (STATUS_SENT, sent_before),
        ).rowcount
        samples = conn.execute(
            "DELETE FROM env_samples WHERE aggregated = 1 AND ts_epoch < ?",
            (samples_before,),
        ).rowcount

    conn.commit()
    conn.execute("VACUUM")
    return {
        "punches": max(punches, 0),
        "readings": max(readings, 0),
        "samples": max(samples, 0),
    }


# --------------------------------------------------------------------------
# Apró állapotok (pl. a legutóbbi karbantartás ideje)
# --------------------------------------------------------------------------
def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else row["value"]


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
