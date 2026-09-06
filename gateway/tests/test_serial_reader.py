"""A SerialReader: a beolvasott sorokból mi kerül a pufferbe és mi nem.

Soros port nélkül: közvetlenül a `handle_line`-t hívjuk, ahogy az olvasó
ciklus tenné.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import db
import serial_reader
from config import USB_DEVICE_IDS
from status import Status


@dataclass
class FakePort:
    """A `serial.tools.list_ports.comports()` egy elemének minimuma."""

    device: str
    vid: int | None = None
    pid: int | None = None
    description: str = ""


def _reader(cfg, conn):
    reader = serial_reader.SerialReader(cfg, Status(), threading.Event())
    reader.conn = conn
    return reader


def test_card_sor_belyegzest_ir_a_pufferbe(cfg, conn):
    reader = _reader(cfg, conn)
    reader.handle_line("CARD;04A1B2C3")

    rows = db.pending_punches(conn, 10)
    assert len(rows) == 1
    assert rows[0]["uid"] == "04A1B2C3"
    # ISO 8601 lokális időbélyeg, offszettel – ezt várja a szerver.
    assert rows[0]["ts_local"].startswith("20")
    assert rows[0]["ts_local"][-6] in "+-"
    # Minden bélyegzés saját uuid4-et kap.
    assert len(rows[0]["event_uuid"]) == 36


def test_ket_kartya_ket_kulon_uuid(cfg, conn):
    reader = _reader(cfg, conn)
    reader.handle_line("CARD;04A1B2C3")
    reader.handle_line("CARD;04A1B2C3")
    uuids = {row["event_uuid"] for row in db.pending_punches(conn, 10)}
    assert len(uuids) == 2


def test_dup_sor_nem_kerul_a_pufferbe(cfg, conn):
    """A debounce az Arduinón történt. Csak logoljuk, nem küldjük fel."""
    reader = _reader(cfg, conn)
    reader.handle_line("DUP;04A1B2C3")
    assert db.pending_punches(conn, 10) == []
    assert db.queue_size(conn) == 0


def test_env_sor_nyers_mintat_ir(cfg, conn):
    reader = _reader(cfg, conn)
    reader.handle_line("ENV;T=23.45;H=41.20;P=1013.25")

    row = conn.execute("SELECT temp, hum, press, aggregated FROM env_samples").fetchone()
    assert (row["temp"], row["hum"], row["press"]) == (23.45, 41.20, 1013.25)
    assert row["aggregated"] == 0
    # A nyers minta nem megy közvetlenül a feltöltési sorba.
    assert db.queue_size(conn) == 0


def test_env_enabled_false_eseten_nincs_szenzoradat(cfg, conn):
    from dataclasses import replace

    reader = _reader(replace(cfg, env_enabled=False), conn)
    reader.handle_line("ENV;T=23.45;H=41.20;P=1013.25")
    assert conn.execute("SELECT COUNT(*) AS n FROM env_samples").fetchone()["n"] == 0


def test_hibas_env_sor_nem_ir_semmit(cfg, conn):
    reader = _reader(cfg, conn)
    reader.handle_line("ENV;T=xx;H=41.20;P=1013.25")
    assert conn.execute("SELECT COUNT(*) AS n FROM env_samples").fetchone()["n"] == 0


def test_szemetes_sor_nem_ir_semmit_es_nem_dob_kivetelt(cfg, conn):
    reader = _reader(cfg, conn)
    for raw in [b"\xff\xfe\x00", "CARD", "", "\x00\x00", "AKARMI;1;2;3", "CARD;04A1\x01"]:
        reader.handle_line(raw)
    assert db.queue_size(conn) == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM env_samples").fetchone()["n"] == 0


def test_err_es_ok_sorok_allitjak_a_heartbeat_allapotot(cfg, conn):
    reader = _reader(cfg, conn)
    reader.status.set_serial_ok(True)
    reader.status.set_bme_ok(True)

    reader.handle_line("ERR;RC522_OFFLINE")
    assert reader.status.serial_ok is False

    reader.handle_line("OK;RC522_RECOVERED")
    assert reader.status.serial_ok is True

    reader.handle_line("ERR;BME_OFFLINE")
    assert reader.status.bme_ok is False

    reader.handle_line("OK;BME_RECOVERED")
    assert reader.status.bme_ok is True


def test_rdy_sorbol_derul_ki_hogy_van_e_szenzor(cfg, conn):
    reader = _reader(cfg, conn)
    reader.handle_line("RDY;fw=1.1.0;rc522=0x92;bme=0x76")
    assert reader.status.bme_ok is True

    reader.handle_line("RDY;fw=1.1.0;rc522=0x92;bme=0x00")
    assert reader.status.bme_ok is False


def test_ismeretlen_err_kod_nem_allit_allapotot(cfg, conn):
    reader = _reader(cfg, conn)
    reader.status.set_serial_ok(True)
    reader.handle_line("ERR;VALAMI_MAS")
    assert reader.status.serial_ok is True


# --------------------------------------------------------------------------
# Portkeresés
# --------------------------------------------------------------------------
def test_a_env_ben_megadott_port_mindig_erosebb(cfg, monkeypatch):
    from dataclasses import replace

    monkeypatch.setattr(
        serial_reader, "available_ports", lambda: [FakePort("COM9", 0x1A86, 0x7523)]
    )
    assert serial_reader.resolve_port(replace(cfg, serial_port="COM5")) == "COM5"


def test_ch340_automatikus_megtalalasa(cfg, monkeypatch):
    monkeypatch.setattr(
        serial_reader,
        "available_ports",
        lambda: [FakePort("COM1", 0x0000, 0x0000), FakePort("COM7", 0x1A86, 0x7523)],
    )
    assert serial_reader.resolve_port(cfg) == "COM7"


def test_minden_ismert_atalakito_megtalalhato(cfg, monkeypatch):
    for (vid, pid), name in USB_DEVICE_IDS.items():
        monkeypatch.setattr(
            serial_reader, "available_ports", lambda vid=vid, pid=pid: [FakePort("COM3", vid, pid)]
        )
        assert serial_reader.resolve_port(cfg) == "COM3", name


def test_ismeretlen_eszkoznel_nincs_talalat(cfg, monkeypatch):
    monkeypatch.setattr(serial_reader, "available_ports", lambda: [FakePort("COM1", 0x1234, 0x5678)])
    assert serial_reader.resolve_port(cfg) is None


def test_ha_nincs_port_akkor_sincs_kivetel(cfg, monkeypatch):
    monkeypatch.setattr(serial_reader, "available_ports", lambda: [])
    assert serial_reader.resolve_port(cfg) is None


def test_foglalt_port_uzenete_magyarul_mondja_meg_mit_kell_tenni():
    message = serial_reader.busy_port_message("COM5", PermissionError("access denied"))
    assert "A COM5 port foglalt." in message
    assert "Arduino IDE Serial Monitor" in message
