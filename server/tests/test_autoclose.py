"""Automatikus napzárás és az induláskori tárhely-ellenőrzés."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.db import SessionLocal, StorageError, ensure_storage
from app.models import WorkSession
from app.services import punches as punch_service
from app.services.autoclose import next_run_utc
from app.timeutil import local_clock_utc, parse_iso_to_utc, to_local, utcnow
from conftest import make_employee, punch_event

UID = "04A1B2C3"


def test_nyitva_felejtett_munkamenet_lezarul(gateway):
    make_employee()
    gateway.events([punch_event(UID, "2026-09-01T08:00:00+02:00")])

    with SessionLocal() as db:
        session = db.scalar(select(WorkSession))
        # A beszúráskori újraszámolás már lezárta, mert a határidő rég elmúlt.
        assert session.auto_closed is True
        assert session.ended_at == parse_iso_to_utc("2026-09-01T23:59:00+02:00")
        assert session.duration_seconds == 15 * 3600 + 59 * 60

        # A háttérfeladat futtatása idempotens: nincs mit zárni.
        assert punch_service.close_stale_sessions(db) == 0


def test_hattermunka_lezarja_a_kezzel_nyitva_hagyott_munkamenetet():
    employee_id = make_employee()
    tegnap = to_local(utcnow()).date() - timedelta(days=1)
    kezdet = local_clock_utc(tegnap, 8, 0)

    with SessionLocal() as db:
        db.add(WorkSession(employee_id=employee_id, started_at=kezdet, auto_closed=False))
        db.commit()

        assert punch_service.close_stale_sessions(db) == 1

        session = db.scalar(select(WorkSession))
        assert session.auto_closed is True
        assert session.ended_at == local_clock_utc(tegnap, 23, 59)


def test_napzaras_elott_nyitott_munkamenet_nem_zarul_le():
    """A napzárás időpontja előtt a nyitott munkamenet nyitva marad.

    Az ellenőrzés pillanatát explicit átadjuk, nem a futtatás óráját
    használjuk – különben a teszt naponta egy percig (23:59 és éjfél között)
    elbukna.
    """
    employee_id = make_employee()
    ma = to_local(utcnow()).date()
    kezdet = local_clock_utc(ma, 8, 0)
    napkozben = local_clock_utc(ma, 17, 0)

    with SessionLocal() as db:
        db.add(WorkSession(employee_id=employee_id, started_at=kezdet, auto_closed=False))
        db.commit()

        assert punch_service.close_stale_sessions(db, now=napkozben) == 0
        assert db.scalar(select(WorkSession)).ended_at is None


def test_kesve_erkezo_kilepes_felulirja_az_automatikus_zarast(gateway):
    """A napzárás nem végleges: ha később megjön a valódi kilépés, az számít."""
    make_employee()
    gateway.events([punch_event(UID, "2026-09-01T08:00:00+02:00")])

    with SessionLocal() as db:
        assert db.scalar(select(WorkSession)).auto_closed is True

    # Az offline pufferből felérkezik az aznapi kilépés.
    gateway.events([punch_event(UID, "2026-09-01T16:30:00+02:00")])

    with SessionLocal() as db:
        session = db.scalar(select(WorkSession))
        assert session.auto_closed is False
        assert session.duration_seconds == 8 * 3600 + 30 * 60
        assert session.out_punch_id is not None


def test_a_kovetkezo_futas_idopontja_a_jovoben_van():
    target = next_run_utc()
    assert target > utcnow()
    local = to_local(target)
    assert (local.hour, local.minute) == (23, 59)


def test_a_kovetkezo_futas_atlep_a_masnapra():
    # Este 23:59:30-kor a mai zárás már elmúlt, a következő holnap van.
    ma = to_local(utcnow()).date()
    kesoi = local_clock_utc(ma, 23, 59) + timedelta(seconds=30)
    assert to_local(next_run_utc(kesoi)).date() == ma + timedelta(days=1)


def test_letrehozhatatlan_mappara_ertheto_hibauzenet(tmp_path):
    """Ha a /data nem mappa (pl. nincs csatolva a volume), érthető üzenet kell."""
    utban_levo_fajl = tmp_path / "data"
    utban_levo_fajl.write_text("ez nem mappa")

    with pytest.raises(StorageError) as excinfo:
        ensure_storage(str(utban_levo_fajl / "iroda.db"))

    uzenet = str(excinfo.value)
    assert "DB_PATH" in uzenet
    assert "/data" in uzenet
    assert "sqlite3" not in uzenet.lower()
    assert "OperationalError" not in uzenet


def test_letezo_irhato_mappa_rendben(tmp_path):
    ensure_storage(str(tmp_path / "iroda.db"))  # nem dob kivételt


def test_ejfel_utani_belepesre_a_kovetkezo_nap_zarasa_vonatkozik():
    kezdet = parse_iso_to_utc("2026-09-01T23:59:30+02:00")
    hatarido = punch_service.auto_close_deadline(kezdet)
    assert hatarido == parse_iso_to_utc("2026-09-02T23:59:00+02:00")
    assert hatarido > kezdet


def test_hajnali_muszak_a_sajat_napjan_zarul():
    kezdet = parse_iso_to_utc("2026-09-01T02:00:00+02:00")
    assert punch_service.auto_close_deadline(kezdet) == parse_iso_to_utc(
        "2026-09-01T23:59:00+02:00"
    )
    assert isinstance(kezdet, datetime)
