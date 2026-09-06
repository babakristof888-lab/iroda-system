"""A soros port olvasása – a SerialReader szál.

A sorrend a legfontosabb tulajdonsága: a beolvasott kártya **előbb kerül a
lemezre**, és csak utána történik bármi hálózati. Ha a gép abban a pillanatban
áramtalanodik, a bélyegzés akkor is megmarad.

Windows-specifikumok, amik a gyakorlatban számítanak:

* a `PermissionError` / `SerialException` a port nyitásakor szinte mindig azt
  jelenti, hogy más program tartja nyitva a portot (tipikusan az Arduino IDE
  Serial Monitora) – erre érthető magyar üzenet megy a logba,
* ha a port menet közben eltűnik (USB kihúzás, vagy a Windows energiatakarékosságból
  lekapcsolta a hubot), a szál **nem áll le**, hanem 2 másodpercenként újranyit.
  A folyamat él tovább, az Uploader közben dolgozik a már pufferelt adatokon.
"""

from __future__ import annotations

import errno
import logging
import sqlite3
import threading
import time
import uuid
from datetime import datetime

import serial
from serial.tools import list_ports

import db
import protocol
from config import (
    SERIAL_BUSY_RETRY_SECONDS,
    SERIAL_PING_SECONDS,
    SERIAL_REOPEN_SECONDS,
    SERIAL_SILENCE_SECONDS,
    USB_DEVICE_IDS,
    Config,
)
from status import Status

log = logging.getLogger("gateway.serial")


# --------------------------------------------------------------------------
# Portkeresés
# --------------------------------------------------------------------------
def available_ports() -> list[object]:
    try:
        return list(list_ports.comports())
    except Exception:
        log.exception("A soros portok listázása nem sikerült")
        return []


def describe_port(port: object) -> str:
    vid = getattr(port, "vid", None)
    pid = getattr(port, "pid", None)
    known = USB_DEVICE_IDS.get((vid, pid)) if vid is not None and pid is not None else None
    ids = f"VID:PID={vid:04X}:{pid:04X}" if vid is not None and pid is not None else "VID:PID=?"
    label = f" – {known}" if known else ""
    return f"{getattr(port, 'device', '?')} ({getattr(port, 'description', '')}, {ids}){label}"


def autodetect_port() -> str | None:
    """Az ismert USB-soros átalakítók (CH340 / FT232 / CP210x) közül az első."""
    ports = available_ports()
    if not ports:
        log.warning("Nem látok egyetlen soros portot sem.")
        return None

    log.debug("Talált soros portok: %s", "; ".join(describe_port(p) for p in ports))
    for port in ports:
        vid = getattr(port, "vid", None)
        pid = getattr(port, "pid", None)
        name = USB_DEVICE_IDS.get((vid, pid))
        if name:
            log.info("Automatikusan megtalált eszköz: %s (%s)", getattr(port, "device", "?"), name)
            return getattr(port, "device", None)

    log.warning(
        "Egyik soros porton sem ismerek fel USB-soros átalakítót. Látott portok: %s. "
        "Ha tudod, melyik az, írd be a .env fájlba: SERIAL_PORT=COM5",
        "; ".join(describe_port(p) for p in ports),
    )
    return None


def resolve_port(cfg: Config) -> str | None:
    """A `.env`-ben megadott port mindig erősebb az automatikus keresésnél."""
    if cfg.serial_port:
        return cfg.serial_port
    return autodetect_port()


# --------------------------------------------------------------------------
# Portnyitás
# --------------------------------------------------------------------------
# A portnyitás háromféleképpen bukhat el, és a teendő mindháromnál más.
OPEN_BUSY = "busy"        # más program tartja nyitva
OPEN_MISSING = "missing"  # nincs ilyen eszköz (kihúzták, vagy más a COM szám)
OPEN_UNKNOWN = "unknown"


def classify_open_error(error: BaseException) -> str:
    """Miért nem nyílt meg a port.

    A `serial.SerialException` a Windows eredeti hibáját szövegként hordozza
    (`PermissionError(13, ...)` / `FileNotFoundError(2, ...)`). Ezek a nevek nem
    fordítódnak le magyar Windowson sem, ezért rájuk lehet szűrni.
    """
    if isinstance(error, PermissionError):
        return OPEN_BUSY
    if isinstance(error, FileNotFoundError):
        return OPEN_MISSING

    code = getattr(error, "errno", None)
    if code == errno.EACCES:
        return OPEN_BUSY
    if code in (errno.ENOENT, errno.ENODEV, errno.ENXIO):
        return OPEN_MISSING

    text = str(error).lower()
    if "permissionerror" in text or "access is denied" in text or "errno 13" in text:
        return OPEN_BUSY
    if "filenotfounderror" in text or "no such file" in text or "errno 2" in text:
        return OPEN_MISSING
    return OPEN_UNKNOWN


def busy_port_message(port: str, error: BaseException) -> str:
    return (
        f"A {port} port foglalt. Zárd be az Arduino IDE Serial Monitorát, vagy más "
        f"programot, ami használja. ({error})"
    )


def missing_port_message(port: str, error: BaseException) -> str:
    return (
        f"A {port} port nem létezik. Nincs bedugva az USB kábel, vagy megváltozott a "
        f"COM-port száma. Nézd meg az Eszközkezelőben (Portok, COM és LPT), és ha más "
        f"a szám, írd át a .env fájlban a SERIAL_PORT sort. ({error})"
    )


def open_failure_message(port: str, error: BaseException) -> str:
    kind = classify_open_error(error)
    if kind == OPEN_BUSY:
        return busy_port_message(port, error)
    if kind == OPEN_MISSING:
        return missing_port_message(port, error)
    return (
        f"A {port} port nem nyitható meg. Vagy más program tartja nyitva (Arduino IDE "
        f"Serial Monitor), vagy nincs bedugva az eszköz. ({error})"
    )


def open_port(cfg: Config, port: str) -> serial.Serial:
    """Portnyitás. A hívó kezeli a kivételt."""
    return serial.Serial(
        port=port,
        baudrate=cfg.baud_rate,
        timeout=1.0,
        write_timeout=2.0,
    )


class SerialReader:
    """A soros port olvasása és a beolvasott sorok pufferbe írása."""

    def __init__(self, cfg: Config, status: Status, stop: threading.Event) -> None:
        self.cfg = cfg
        self.status = status
        self.stop = stop
        self.conn: sqlite3.Connection | None = None
        self.port: serial.Serial | None = None
        self._last_line_at = 0.0
        self._last_ping_at = 0.0

    # -- adatbázis ---------------------------------------------------------
    def _db(self) -> sqlite3.Connection:
        if self.conn is None:
            self.conn = db.connect(self.cfg.db_path)
            db.init_schema(self.conn)
        return self.conn

    # -- a sorok feldolgozása ---------------------------------------------
    def handle_line(self, raw: str | bytes) -> None:
        line = protocol.parse_line(raw)
        if line is None:
            return

        if line.kind == protocol.CARD:
            self._store_card(line.uid or "")
        elif line.kind == protocol.ENV:
            self._store_env(line)
        elif line.kind == protocol.DUP:
            # A debounce az Arduinón történt: csak naplózzuk, nem küldjük fel.
            log.info("Ismételt olvasás a debounce ablakon belül (nem töltöm fel): %s", line.uid)
        elif line.kind == protocol.READY:
            self._handle_ready(line)
        elif line.kind == protocol.PONG:
            log.debug("PONG az eszköztől (firmware: %s)", line.firmware or "?")
        elif line.kind == protocol.ERROR:
            self._handle_error(line)
        elif line.kind == protocol.OK:
            self._handle_ok(line)
        else:
            log.debug("Eldobott soros sor (%s): %r", line.reason, line.raw)

    def _store_card(self, uid: str) -> None:
        """A bélyegzés lemezre írása. Előbb a lemez, aztán minden más."""
        now = datetime.now(self.cfg.tz)
        event_uuid = str(uuid.uuid4())
        ts_local = now.isoformat()
        try:
            db.insert_punch(self._db(), event_uuid, uid, ts_local, now.timestamp())
        except sqlite3.Error:
            log.exception("A bélyegzés mentése nem sikerült! UID=%s ts=%s", uid, ts_local)
            return
        log.info("Kártya olvasva: UID=%s idő=%s uuid=%s", uid, ts_local, event_uuid)

    def _store_env(self, line: protocol.Line) -> None:
        if not self.cfg.env_enabled:
            log.debug("ENV_ENABLED=false, a mérést eldobom: %r", line.raw)
            return
        now = datetime.now(self.cfg.tz)
        try:
            db.insert_env_sample(
                self._db(), now.isoformat(), now.timestamp(), line.temp, line.hum, line.press
            )
        except sqlite3.Error:
            log.exception("A környezeti minta mentése nem sikerült: %r", line.raw)
            return
        # Percenként jön: csak DEBUG szinten, különben tele lesz a naplófájl.
        log.debug(
            "Környezeti minta: %.2f °C, %.2f %%, %.2f hPa", line.temp, line.hum, line.press
        )

    def _handle_ready(self, line: protocol.Line) -> None:
        log.info(
            "Az eszköz elindult. Firmware: %s, RC522: %s, BME280: %s",
            line.firmware or "?",
            line.fields.get("rc522", "?"),
            line.fields.get("bme", "?"),
        )
        rc522 = line.rc522_present
        if rc522 is not None:
            self.status.set_serial_ok(rc522)
            if not rc522:
                log.warning("Az eszköz azt jelenti, hogy az RC522 olvasó nincs meg.")
        bme = line.bme_present
        if bme is not None:
            self.status.set_bme_ok(bme)
            if not bme:
                log.warning("Az eszköz azt jelenti, hogy a BME280 szenzor nincs meg.")

    def _handle_error(self, line: protocol.Line) -> None:
        if line.code == protocol.ERR_RC522_OFFLINE:
            self.status.set_serial_ok(False)
            log.warning("Az RFID olvasó nem válaszol (ERR;RC522_OFFLINE).")
        elif line.code == protocol.ERR_BME_OFFLINE:
            self.status.set_bme_ok(False)
            log.warning("A BME280 szenzor nem válaszol (ERR;BME_OFFLINE).")
        else:
            log.warning("Az eszköz hibát jelez: %s", line.code)

    def _handle_ok(self, line: protocol.Line) -> None:
        if line.code == protocol.OK_RC522_RECOVERED:
            self.status.set_serial_ok(True)
            log.info("Az RFID olvasó magához tért (OK;RC522_RECOVERED).")
        elif line.code == protocol.OK_BME_RECOVERED:
            self.status.set_bme_ok(True)
            log.info("A BME280 szenzor visszajött (OK;BME_RECOVERED).")
        else:
            log.info("Az eszköz üzenete: OK;%s", line.code)

    # -- parancsküldés -----------------------------------------------------
    def send_command(self, command: str) -> bool:
        if self.port is None:
            return False
        try:
            self.port.write(f"{command}\n".encode("ascii"))
            self.port.flush()
            return True
        except (serial.SerialException, OSError) as exc:
            log.warning("A(z) %s parancs küldése nem sikerült: %s", command, exc)
            return False

    # -- a szál törzse -----------------------------------------------------
    def _read_loop(self) -> None:
        """Olvasás, amíg a port él. Kilépéskor a hívó újranyit."""
        assert self.port is not None
        now = time.time()
        self._last_line_at = now
        self._last_ping_at = now

        while not self.stop.is_set():
            data = self.port.readline()
            now = time.time()
            if data:
                self._last_line_at = now
                self.handle_line(data)
            elif now - self._last_line_at > SERIAL_SILENCE_SECONDS:
                # Nyitva van a port, de az eszköz néma – lefagyott vagy elment.
                log.warning(
                    "Az eszköz %.0f másodperce nem szólt semmit, újranyitom a portot.",
                    now - self._last_line_at,
                )
                return
            if now - self._last_ping_at >= SERIAL_PING_SECONDS:
                self._last_ping_at = now
                self.send_command(protocol.CMD_PING)

    def run(self) -> None:
        try:
            self._db()
            log.info("SerialReader elindult")
            while not self.stop.is_set():
                port_name = resolve_port(self.cfg)
                if port_name is None:
                    self.status.set_serial_ok(False)
                    self.stop.wait(SERIAL_BUSY_RETRY_SECONDS)
                    continue

                try:
                    self.port = open_port(self.cfg, port_name)
                except (PermissionError, serial.SerialException, OSError) as exc:
                    self.status.set_serial_ok(False)
                    kind = classify_open_error(exc)
                    if kind == OPEN_MISSING:
                        # Az eszköz eltűnt (kihúzás, vagy a Windows lekapcsolta a
                        # hubot): sűrűbben nézzük, hogy visszajött-e.
                        log.warning(open_failure_message(port_name, exc))
                        self.stop.wait(SERIAL_REOPEN_SECONDS)
                    else:
                        log.error(open_failure_message(port_name, exc))
                        self.stop.wait(SERIAL_BUSY_RETRY_SECONDS)
                    continue

                log.info("A %s port megnyitva, %d baud.", port_name, self.cfg.baud_rate)
                self.status.set_serial_ok(True)
                self.status.set_port(port_name)
                try:
                    self._read_loop()
                except (serial.SerialException, OSError) as exc:
                    log.warning(
                        "A %s port elveszett (%s). %.0f másodperc múlva újrapróbálom. "
                        "Ha kihúztad az USB kábelt, dugd vissza – a puffer közben megmarad.",
                        port_name,
                        exc,
                        SERIAL_REOPEN_SECONDS,
                    )
                finally:
                    self.status.set_serial_ok(False)
                    self.status.set_port(None)
                    self._close_port()
                self.stop.wait(SERIAL_REOPEN_SECONDS)
        finally:
            self._close_port()
            if self.conn is not None:
                self.conn.close()
                self.conn = None

    def _close_port(self) -> None:
        if self.port is not None:
            try:
                self.port.close()
            except Exception:
                pass
            self.port = None


def run(cfg: Config, status: Status, stop: threading.Event) -> None:
    """A SerialReader szál belépési pontja."""
    SerialReader(cfg, status, stop).run()
