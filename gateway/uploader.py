"""Feltöltés a szerverre – az Uploader szál.

Ez a szál viszi a periodikus, hálózatot használó munkát:

1. **bélyegzések** (legfeljebb 100, a legrégebbiek) -> `POST /api/v1/events`
2. **órás összesítők** (legfeljebb 50) -> `POST /api/v1/env`
3. percenként **heartbeat** -> `POST /api/v1/heartbeat`
4. naponta egyszer **karbantartás** (régi sorok törlése + VACUUM)

A heartbeat szándékosan nem függ a feltöltés backoffjától: pont akkor a
legfontosabb, hogy az admin lássa a gateway-t, amikor a feltöltés akad.

A válasz feldolgozása mindkét végpontnál azonos:

* `accepted` **és** `duplicates` -> `sent` (mindkettő sikeres feldolgozás),
* `errors` -> `error`, az okkal együtt; ezeket soha nem próbáljuk újra,
  mert sosem fognának sikerülni,
* hálózati hiba / 5xx / timeout -> minden marad `pending`, jön a backoff.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Sequence

import requests

import db
from config import (
    BACKOFF_MAX_SECONDS,
    CONNECT_TIMEOUT_SECONDS,
    ENV_BATCH_SIZE,
    HEARTBEAT_INTERVAL_SECONDS,
    MAINTENANCE_INTERVAL_SECONDS,
    PUNCH_BATCH_SIZE,
    READ_TIMEOUT_SECONDS,
    SAMPLE_RETENTION_DAYS,
    SENT_RETENTION_DAYS,
    UPLOAD_INTERVAL_SECONDS,
    Config,
)
from status import Status

log = logging.getLogger("gateway.uploader")

TIMEOUT = (CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS)
TICK_SECONDS = 1.0
META_LAST_MAINTENANCE = "last_maintenance_epoch"

# A ciklus kimenetele
IDLE = "idle"        # nem volt mit küldeni
OK = "ok"            # a szerver feldolgozta a köteget
FAIL = "fail"        # hálózati hiba / 5xx / timeout / értelmezhetetlen válasz


def backoff_delay(failures: int) -> float:
    """5s -> 10s -> 20s -> 40s -> ... -> max 300s.

    `failures` az egymás utáni sikertelen kísérletek száma. Nulla (vagy az utána
    következő siker) esetén a normál 5 másodperces ütem jön vissza.
    """
    if failures <= 0:
        return float(UPLOAD_INTERVAL_SECONDS)
    return float(min(UPLOAD_INTERVAL_SECONDS * (2 ** (failures - 1)), BACKOFF_MAX_SECONDS))


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def classify_response(body: Any, uuid_key: str, batch_uuids: Sequence[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """A szerver válaszából: mit jelöljünk `sent`-nek és mit `error`-nak.

    Az `accepted` és a `duplicates` **egyaránt sikeres feldolgozást jelent** –
    a duplikátum azt jelenti, hogy a szerver már megkapta, csak a válasz veszett
    el. Újraküldeni értelmetlen lenne.

    Amit a szerver egyik listában sem említ, az `pending` marad: a következő
    körben újra megpróbáljuk.
    """
    known = set(batch_uuids)
    if not isinstance(body, dict):
        return [], []

    sent = [
        value
        for value in _string_list(body.get("accepted")) + _string_list(body.get("duplicates"))
        if value in known
    ]

    failures: list[tuple[str, str]] = []
    raw_errors = body.get("errors")
    if isinstance(raw_errors, list):
        for item in raw_errors:
            if not isinstance(item, dict):
                continue
            value = item.get(uuid_key)
            if not isinstance(value, str) or value not in known:
                continue
            reason = item.get("reason")
            failures.append((value, str(reason) if reason else "ismeretlen ok"))

    # Ha egy uuid mindkét listában szerepel, a siker az erősebb.
    sent_set = set(sent)
    failures = [item for item in failures if item[0] not in sent_set]
    return sent, failures


@dataclass
class BatchResult:
    outcome: str
    sent: int = 0
    errors: int = 0
    remaining: int = 0


class Uploader:
    def __init__(
        self,
        cfg: Config,
        status: Status,
        conn: sqlite3.Connection,
        session: requests.Session | None = None,
    ) -> None:
        self.cfg = cfg
        self.status = status
        self.conn = conn
        self.session = session if session is not None else requests.Session()
        self.failures = 0

    # -- HTTP --------------------------------------------------------------
    @property
    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self.cfg.api_key, "Content-Type": "application/json"}

    def _post(self, url: str, payload: dict[str, Any]) -> Any | None:
        """`None`, ha a köteget nem sikerült feldolgoztatni.

        Ilyenkor semmit nem írunk át: a sorok `pending`-ben maradnak, és jön a
        backoff. Inkább küldjük fel kétszer, mint hogy elvesszen.
        """
        try:
            response = self.session.post(url, json=payload, headers=self._headers, timeout=TIMEOUT)
        except requests.RequestException as exc:
            log.warning("Hálózati hiba (%s): %s", url, exc)
            return None

        if response.status_code in (401, 403):
            log.error(
                "A szerver elutasította a kulcsot (HTTP %s). Ellenőrizd, hogy a .env "
                "GATEWAY_API_KEY értéke pontosan ugyanaz-e, mint a Railway-en beállított. "
                "A puffer addig gyűlik, semmi nem vész el.",
                response.status_code,
            )
            return None
        if response.status_code == 413:
            log.error(
                "A szerver túl nagynak találta a köteget (HTTP 413). Ez konfigurációs "
                "hiba, szólj a fejlesztőnek."
            )
            return None
        if response.status_code >= 500:
            log.warning("A szerver hibát adott (HTTP %s): %s", response.status_code, url)
            return None
        if response.status_code >= 400:
            log.warning(
                "A szerver visszautasította a kérést (HTTP %s): %s",
                response.status_code,
                response.text[:300],
            )
            return None

        try:
            return response.json()
        except ValueError:
            log.warning("A szerver válasza nem értelmezhető JSON: %s", response.text[:300])
            return None

    # -- kötegek -----------------------------------------------------------
    def _upload_batch(
        self,
        table: str,
        rows: Sequence[sqlite3.Row],
        url: str,
        payload: dict[str, Any],
        uuid_key: str,
        label: str,
    ) -> BatchResult:
        batch_uuids = [row[uuid_key] for row in rows]
        body = self._post(url, payload)
        if body is None:
            db.bump_attempts(self.conn, table, batch_uuids)
            remaining = db.count_by_status(self.conn, table, db.STATUS_PENDING)
            log.info(
                "%s: %d elem feltöltése nem sikerült, marad a pufferben (%d vár összesen)",
                label,
                len(batch_uuids),
                remaining,
            )
            return BatchResult(FAIL, remaining=remaining)

        sent, failures = classify_response(body, uuid_key, batch_uuids)
        now_iso = datetime.now(self.cfg.tz).isoformat()
        db.mark_sent(self.conn, table, sent, now_iso)
        db.mark_error(self.conn, table, failures)
        stuck = [value for value in batch_uuids if value not in set(sent) | {f[0] for f in failures}]
        if stuck:
            db.bump_attempts(self.conn, table, stuck)

        remaining = db.count_by_status(self.conn, table, db.STATUS_PENDING)
        log.info(
            "%s: %d feltöltve, %d hibás, %d válasz nélkül maradt, %d vár még",
            label,
            len(sent),
            len(failures),
            len(stuck),
            remaining,
        )
        for value, reason in failures:
            log.warning("%s: a szerver visszautasította (%s): %s", label, reason, value)
        return BatchResult(OK, sent=len(sent), errors=len(failures), remaining=remaining)

    def upload_punches(self) -> BatchResult:
        rows = db.pending_punches(self.conn, PUNCH_BATCH_SIZE)
        if not rows:
            return BatchResult(IDLE)
        payload = {
            "gateway_id": self.cfg.gateway_id,
            "events": [
                {
                    "event_uuid": row["event_uuid"],
                    "uid": row["uid"],
                    "ts_local": row["ts_local"],
                }
                for row in rows
            ],
        }
        return self._upload_batch(
            db.PUNCH_TABLE, rows, self.cfg.events_url, payload, "event_uuid", "Bélyegzések"
        )

    def upload_env(self) -> BatchResult:
        rows = db.pending_env_readings(self.conn, ENV_BATCH_SIZE)
        if not rows:
            return BatchResult(IDLE)
        payload = {
            "gateway_id": self.cfg.gateway_id,
            "readings": [
                {
                    "reading_uuid": row["reading_uuid"],
                    "sensor_id": row["sensor_id"],
                    "period_start": row["period_start"],
                    "period_end": row["period_end"],
                    "sample_count": row["sample_count"],
                    "temp_avg": row["temp_avg"],
                    "temp_min": row["temp_min"],
                    "temp_max": row["temp_max"],
                    "hum_avg": row["hum_avg"],
                    "hum_min": row["hum_min"],
                    "hum_max": row["hum_max"],
                    "press_avg": row["press_avg"],
                }
                for row in rows
            ],
        }
        return self._upload_batch(
            db.ENV_TABLE, rows, self.cfg.env_url, payload, "reading_uuid", "Környezeti mérések"
        )

    def upload_once(self) -> str:
        """Egy feltöltési kör. A bélyegzések mennek először.

        Ha a bélyegzés-köteg hálózati hibán elhasal, ugyanabban a körben meg sem
        próbáljuk az env köteget: úgyis ugyanaz lenne az eredmény, csak a
        timeoutra várnánk még egyszer.
        """
        punches = self.upload_punches()
        if punches.outcome == FAIL:
            return FAIL
        readings = self.upload_env()
        if readings.outcome == FAIL:
            return FAIL
        return OK if OK in (punches.outcome, readings.outcome) else IDLE

    # -- heartbeat ---------------------------------------------------------
    def heartbeat(self) -> bool:
        try:
            size = db.queue_size(self.conn)
        except sqlite3.Error:
            log.exception("A pufferméret lekérdezése nem sikerült a heartbeathez")
            size = None

        payload = {
            "gateway_id": self.cfg.gateway_id,
            "version": self.cfg.version,
            "queue_size": size,
            "serial_ok": self.status.serial_ok,
            "bme_ok": self.status.bme_ok,
        }
        try:
            response = self.session.post(
                self.cfg.heartbeat_url, json=payload, headers=self._headers, timeout=TIMEOUT
            )
        except requests.RequestException as exc:
            log.warning("A heartbeat nem ment fel: %s", exc)
            return False

        if response.status_code >= 400:
            log.warning("A heartbeatre HTTP %s jött", response.status_code)
            return False
        log.debug(
            "Heartbeat elküldve (queue_size=%s, serial_ok=%s, bme_ok=%s)",
            size,
            payload["serial_ok"],
            payload["bme_ok"],
        )
        return True

    # -- karbantartás ------------------------------------------------------
    def maintenance(self, now_epoch: float) -> None:
        try:
            removed = db.cleanup(
                self.conn, now_epoch, SENT_RETENTION_DAYS, SAMPLE_RETENTION_DAYS
            )
            db.set_meta(self.conn, META_LAST_MAINTENANCE, str(now_epoch))
        except sqlite3.Error:
            log.exception("A karbantartás nem futott le")
            return
        log.info(
            "Karbantartás kész: %d feltöltött bélyegzés, %d feltöltött mérés és %d nyers minta "
            "törölve, az adatbázis tömörítve. (A hibás sorokat soha nem töröljük.)",
            removed["punches"],
            removed["readings"],
            removed["samples"],
        )

    def maintenance_due(self, now_epoch: float) -> bool:
        raw = db.get_meta(self.conn, META_LAST_MAINTENANCE)
        if raw is None:
            return True
        try:
            last = float(raw)
        except ValueError:
            return True
        return now_epoch - last >= MAINTENANCE_INTERVAL_SECONDS


def run(cfg: Config, status: Status, stop: threading.Event) -> None:
    """Az Uploader szál törzse: másodpercenként ébred, és eldönti, mi esedékes."""
    conn = db.connect(cfg.db_path)
    try:
        db.init_schema(conn)
        uploader = Uploader(cfg, status, conn)
        log.info(
            "Uploader elindult (%s, gateway_id=%s)", cfg.server_url, cfg.gateway_id
        )
        if not cfg.api_key:
            log.error(
                "A GATEWAY_API_KEY üres a .env fájlban – a szerver minden hívást 401-gyel "
                "utasít vissza. Az adat addig is gyűlik a pufferben."
            )

        next_upload = 0.0
        next_heartbeat = 0.0

        while not stop.is_set():
            now = time.time()

            if now >= next_upload:
                outcome = uploader.upload_once()
                if outcome == FAIL:
                    uploader.failures += 1
                    delay = backoff_delay(uploader.failures)
                    log.info(
                        "Sikertelen feltöltés (%d. egymás után), %.0f másodperc múlva próbálom újra",
                        uploader.failures,
                        delay,
                    )
                else:
                    if uploader.failures:
                        log.info("A kapcsolat helyreállt, vissza a normál ütemre.")
                    uploader.failures = 0
                    delay = float(UPLOAD_INTERVAL_SECONDS)
                next_upload = time.time() + delay

            if now >= next_heartbeat:
                uploader.heartbeat()
                next_heartbeat = time.time() + HEARTBEAT_INTERVAL_SECONDS

            if uploader.maintenance_due(now):
                uploader.maintenance(now)

            stop.wait(TICK_SECONDS)
    finally:
        conn.close()
