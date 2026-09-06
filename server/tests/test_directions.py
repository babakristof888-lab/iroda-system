"""Az üzleti logika magja: IN/OUT irány, munkamenetek, késve érkező események.

Ezek a tesztek védik azt a tulajdonságot, ami miatt a rendszer működik:
a bélyegzéseket MINDIG `ts_utc` szerint dolgozzuk fel, nem abban a
sorrendben, ahogy megérkeztek.
"""

from __future__ import annotations

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Punch, WorkSession
from app.services import punches as punch_service
from app.timeutil import parse_iso_to_utc
from conftest import make_employee, punch_event

UID = "04A1B2C3"


def _punches() -> list[Punch]:
    with SessionLocal() as db:
        return list(db.scalars(select(Punch).order_by(Punch.ts_utc, Punch.id)))


def _sessions() -> list[WorkSession]:
    with SessionLocal() as db:
        return list(db.scalars(select(WorkSession).order_by(WorkSession.started_at)))


def _directions() -> list[str | None]:
    return [punch.direction for punch in _punches()]


def test_in_out_valtakozik_helyes_sorrendben(gateway):
    make_employee()
    gateway.events(
        [
            punch_event(UID, "2026-09-01T08:00:00+02:00"),
            punch_event(UID, "2026-09-01T12:00:00+02:00"),
            punch_event(UID, "2026-09-01T13:00:00+02:00"),
            punch_event(UID, "2026-09-01T17:00:00+02:00"),
        ]
    )

    assert _directions() == ["IN", "OUT", "IN", "OUT"]

    sessions = _sessions()
    assert len(sessions) == 2
    assert [s.duration_seconds for s in sessions] == [4 * 3600, 4 * 3600]
    assert all(not s.auto_closed for s in sessions)


def test_kesve_erkezo_esemeny_ujraszamolja_az_iranyokat_es_a_munkameneteket(gateway):
    """A prompt konkrét esete: 08:00 IN, 16:00 OUT, majd utólag beesik egy 12:00-s punch.

    A 12:00-s eseményt a gateway offline pufferéből küldi fel, tehát később
    érkezik, mint a 16:00-s – de korábban keletkezett. A feldolgozás után:
    08:00 IN, 12:00 OUT, 16:00 IN.
    """
    make_employee()

    gateway.events([punch_event(UID, "2026-09-01T08:00:00+02:00")])
    gateway.events([punch_event(UID, "2026-09-01T16:00:00+02:00")])

    # Eddig a szokásos: egy 8 órás munkamenet.
    assert _directions() == ["IN", "OUT"]
    assert [s.duration_seconds for s in _sessions()] == [8 * 3600]

    # És most beesik az offline pufferből a 12:00-s bélyegzés.
    gateway.events([punch_event(UID, "2026-09-01T12:00:00+02:00")])

    assert _directions() == ["IN", "OUT", "IN"]

    sessions = _sessions()
    assert len(sessions) == 2

    # 08:00 -> 12:00, valódi kilépéssel lezárva.
    assert sessions[0].started_at == parse_iso_to_utc("2026-09-01T08:00:00+02:00")
    assert sessions[0].ended_at == parse_iso_to_utc("2026-09-01T12:00:00+02:00")
    assert sessions[0].duration_seconds == 4 * 3600
    assert sessions[0].auto_closed is False

    # 16:00-kor újra belépett, kilépés nélkül -> a napzárás zárta le 23:59-kor.
    assert sessions[1].started_at == parse_iso_to_utc("2026-09-01T16:00:00+02:00")
    assert sessions[1].auto_closed is True
    assert sessions[1].ended_at == parse_iso_to_utc("2026-09-01T23:59:00+02:00")
    assert sessions[1].duration_seconds == 7 * 3600 + 59 * 60


def test_kesve_erkezo_koteg_tobb_esemennyel(gateway):
    """Három óra offline után egyszerre érkezik fel négy korábbi esemény."""
    make_employee()

    # A friss esemény ér ide először.
    gateway.events([punch_event(UID, "2026-09-01T17:00:00+02:00")])
    assert _directions() == ["IN"]

    # Majd a puffer tartalma, ami mind korábbi.
    gateway.events(
        [
            punch_event(UID, "2026-09-01T13:00:00+02:00"),
            punch_event(UID, "2026-09-01T08:00:00+02:00"),
            punch_event(UID, "2026-09-01T12:00:00+02:00"),
        ]
    )

    assert _directions() == ["IN", "OUT", "IN", "OUT"]
    sessions = _sessions()
    assert len(sessions) == 2
    assert [s.duration_seconds for s in sessions] == [4 * 3600, 4 * 3600]
    assert not any(s.auto_closed for s in sessions)


def test_debounce_60_masodpercen_belul_nem_nyit_munkamenetet(gateway):
    make_employee()
    gateway.events(
        [
            punch_event(UID, "2026-09-01T08:00:00+02:00"),
            punch_event(UID, "2026-09-01T08:00:30+02:00"),
            punch_event(UID, "2026-09-01T08:00:45+02:00"),
        ]
    )

    punches = _punches()
    assert punches[0].direction == "IN"
    assert punches[1].direction is None
    assert punches[2].direction is None
    assert "debounced" in punches[1].note
    assert "debounced" in punches[2].note

    # Egyetlen munkamenet nyílt, a gyors ismétlések nem zárták le.
    sessions = _sessions()
    assert len(sessions) == 1
    assert sessions[0].auto_closed is True  # kilépés nem volt, a napzárás zárta


def test_debounce_kuszob_utan_mar_valodi_kilepes(gateway):
    make_employee()
    gateway.events(
        [
            punch_event(UID, "2026-09-01T08:00:00+02:00"),
            punch_event(UID, "2026-09-01T08:00:30+02:00"),  # debounce
            punch_event(UID, "2026-09-01T08:02:00+02:00"),  # 120 mp -> valódi
        ]
    )
    assert _directions() == ["IN", None, "OUT"]
    sessions = _sessions()
    assert len(sessions) == 1
    assert sessions[0].duration_seconds == 120


def test_ismeretlen_uid_nem_vesz_el_es_utolag_feldolgozodik(gateway):
    ismeretlen = "DEADBEEF"
    gateway.events(
        [
            punch_event(ismeretlen, "2026-09-01T08:00:00+02:00"),
            punch_event(ismeretlen, "2026-09-01T16:00:00+02:00"),
        ]
    )

    # A bélyegzések megvannak, csak nem tartoznak senkihez.
    punches = _punches()
    assert len(punches) == 2
    assert all(punch.employee_id is None for punch in punches)
    assert all(punch.direction is None for punch in punches)
    assert _sessions() == []

    with SessionLocal() as db:
        unknown = punch_service.unknown_card_uids(db)
        assert [item["uid"] for item in unknown] == [ismeretlen]
        assert unknown[0]["count"] == 2

    # Az adminban hozzárendelik egy dolgozóhoz.
    employee_id = make_employee(uid=None)
    with SessionLocal() as db:
        card, backfilled = punch_service.assign_card(db, ismeretlen, employee_id)
        assert card.employee_id == employee_id
        assert backfilled == 2

    # Visszamenőleg megkapták a dolgozót, és lefutott rájuk az irányszámítás.
    punches = _punches()
    assert [punch.employee_id for punch in punches] == [employee_id, employee_id]
    assert [punch.direction for punch in punches] == ["IN", "OUT"]

    sessions = _sessions()
    assert len(sessions) == 1
    assert sessions[0].duration_seconds == 8 * 3600

    with SessionLocal() as db:
        assert punch_service.unknown_card_uids(db) == []


def test_letiltott_kartya_bejegyzese_ismeretlenkent_kerul_be(gateway):
    employee_id = make_employee()
    with SessionLocal() as db:
        from app.models import Card

        card = db.scalar(select(Card).where(Card.uid == UID))
        card.active = False
        db.commit()

    gateway.events([punch_event(UID, "2026-09-01T08:00:00+02:00")])

    punch = _punches()[0]
    assert punch.employee_id is None
    assert punch.direction is None
    assert employee_id  # a dolgozó létezik, csak a kártyája le van tiltva


def test_automatikus_zaras_kulon_napokra_bontja_a_belyegzeseket(gateway):
    """Ha valaki nem jelentkezik ki, a másnapi belépés nem lesz kilépés."""
    make_employee()
    gateway.events(
        [
            punch_event(UID, "2026-09-01T08:00:00+02:00"),
            punch_event(UID, "2026-09-02T08:00:00+02:00"),
        ]
    )

    # Mindkettő belépés: az elsőt a napzárás zárta le, nem a másnapi bélyegzés.
    assert _directions() == ["IN", "IN"]
    sessions = _sessions()
    assert len(sessions) == 2
    assert sessions[0].auto_closed is True
    assert sessions[0].ended_at == parse_iso_to_utc("2026-09-01T23:59:00+02:00")
    assert sessions[1].auto_closed is True
    assert sessions[1].ended_at == parse_iso_to_utc("2026-09-02T23:59:00+02:00")


def test_ket_dolgozo_nem_zavarja_egymast(gateway):
    make_employee(name="Első Elek", code="E001", uid="AAAA1111")
    make_employee(name="Második Mária", code="E002", uid="BBBB2222")

    gateway.events(
        [
            punch_event("AAAA1111", "2026-09-01T08:00:00+02:00"),
            punch_event("BBBB2222", "2026-09-01T09:00:00+02:00"),
            punch_event("AAAA1111", "2026-09-01T16:00:00+02:00"),
            punch_event("BBBB2222", "2026-09-01T17:00:00+02:00"),
        ]
    )

    assert _directions() == ["IN", "IN", "OUT", "OUT"]
    sessions = _sessions()
    assert [s.duration_seconds for s in sessions] == [8 * 3600, 8 * 3600]
