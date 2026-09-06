"""Nyári/téli időszámítás váltása.

Magyarországon 2026-ban március 29-én hajnali 02:00-kor előre (a nap 23 órás),
2025-ben október 26-án hajnali 03:00-kor vissza (a nap 25 órás) állítjuk az órát.
A ledolgozott idő ilyenkor is a VALÓBAN eltelt idő legyen, nem a faliórán
látszó különbség.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select

from app.db import SessionLocal
from app.models import WorkSession
from app.services import reports as report_service
from app.timeutil import (
    local_date_of,
    local_day_bounds_utc,
    parse_iso_to_utc,
)
from conftest import make_employee, punch_event

UID = "04A1B2C3"


def _sessions() -> list[WorkSession]:
    with SessionLocal() as db:
        return list(db.scalars(select(WorkSession).order_by(WorkSession.started_at)))


def test_tavaszi_oraatallitas_napja_23_oras(gateway):
    """2026-03-29: 01:30-tól 05:30-ig a faliórán 4 óra, valójában 3."""
    make_employee()
    gateway.events(
        [
            punch_event(UID, "2026-03-29T01:30:00+01:00"),  # még CET
            punch_event(UID, "2026-03-29T05:30:00+02:00"),  # már CEST
        ]
    )

    sessions = _sessions()
    assert len(sessions) == 1
    assert sessions[0].duration_seconds == 3 * 3600
    assert local_date_of(sessions[0].started_at) == date(2026, 3, 29)


def test_oszi_oraatallitas_napja_25_oras(gateway):
    """2025-10-26: 01:30-tól 04:30-ig a faliórán 3 óra, valójában 4."""
    make_employee()
    gateway.events(
        [
            punch_event(UID, "2025-10-26T01:30:00+02:00"),  # még CEST
            punch_event(UID, "2025-10-26T04:30:00+01:00"),  # már CET
        ]
    )

    sessions = _sessions()
    assert len(sessions) == 1
    assert sessions[0].duration_seconds == 4 * 3600


def test_normal_munkanap_az_oraatallitas_napjan(gateway):
    """A hajnali váltás nem befolyásolja a szokásos 8-16-os műszakot."""
    make_employee()
    gateway.events(
        [
            punch_event(UID, "2026-03-29T08:00:00+02:00"),
            punch_event(UID, "2026-03-29T16:00:00+02:00"),
        ]
    )
    assert _sessions()[0].duration_seconds == 8 * 3600


def test_a_lokalis_nap_hatarai_kovetik_az_oraatallitast():
    tavaszi_start, tavaszi_end = local_day_bounds_utc(date(2026, 3, 29))
    assert (tavaszi_end - tavaszi_start).total_seconds() == 23 * 3600

    oszi_start, oszi_end = local_day_bounds_utc(date(2025, 10, 26))
    assert (oszi_end - oszi_start).total_seconds() == 25 * 3600

    hetkoznap_start, hetkoznap_end = local_day_bounds_utc(date(2026, 9, 1))
    assert (hetkoznap_end - hetkoznap_start).total_seconds() == 24 * 3600


def test_riport_az_oraatallitas_napjan_a_helyes_orat_mutatja(gateway):
    make_employee()
    gateway.events(
        [
            punch_event(UID, "2026-03-29T01:30:00+01:00"),
            punch_event(UID, "2026-03-29T05:30:00+02:00"),
        ]
    )

    with SessionLocal() as db:
        rows = report_service.collect_sessions(db, date(2026, 3, 29), date(2026, 3, 29))

    assert len(rows) == 1
    assert rows[0].hours == 3.0
    assert rows[0].local_date == date(2026, 3, 29)

    days = report_service.group_daily(rows)
    assert len(days) == 1
    assert days[0].seconds == 3 * 3600

    months = report_service.group_monthly(rows)
    assert months[0].month == "2026-03"
    assert months[0].hours == 3.0


def test_a_belyegzes_a_helyes_lokalis_naphoz_tartozik(gateway):
    """Az őszi váltás előtti 01:30 még ugyanaznap van, akkor is, ha UTC-ben előző nap."""
    make_employee()
    gateway.events([punch_event(UID, "2025-10-26T01:30:00+02:00")])

    session = _sessions()[0]
    # UTC-ben ez még október 25. 23:30, lokálisan viszont már 26-a.
    assert session.started_at == parse_iso_to_utc("2025-10-25T23:30:00+00:00")
    assert local_date_of(session.started_at) == date(2025, 10, 26)

    with SessionLocal() as db:
        rows = report_service.collect_sessions(db, date(2025, 10, 26), date(2025, 10, 26))
    assert len(rows) == 1
