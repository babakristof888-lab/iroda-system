"""Statisztika: időszakok, napi óraszám, dolgozók összehasonlítása."""

from __future__ import annotations

from datetime import date

from app.db import SessionLocal
from app.services import stats as stats_service
from app.timeutil import parse_iso_to_utc
from conftest import make_employee, punch_event
from test_salary import make_session

UID = "04A1B2C3"


# --------------------------------------------------------------------------
# Időszak
# --------------------------------------------------------------------------
def test_a_het_hetfotol_vasarnapig_tart():
    # 2026-09-09 szerda
    period = stats_service.period_of(date(2026, 9, 9), "week")
    assert period.first == date(2026, 9, 7)  # hétfő
    assert period.last == date(2026, 9, 13)  # vasárnap
    assert len(period.days) == 7


def test_a_honap_a_teljes_naptari_honap():
    period = stats_service.period_of(date(2026, 9, 9), "month")
    assert (period.first, period.last) == (date(2026, 9, 1), date(2026, 9, 30))
    assert len(period.days) == 30

    februar = stats_service.period_of(date(2026, 2, 10), "month")
    assert februar.last == date(2026, 2, 28)


def test_az_idoszak_leptetese_atfordul_ev_hataran():
    december = stats_service.period_of(date(2026, 12, 15), "month")
    assert december.shifted(1).first == date(2027, 1, 1)
    assert december.shifted(-1).first == date(2026, 11, 1)

    het = stats_service.period_of(date(2027, 1, 1), "week")
    assert het.shifted(-1).first == het.first - __import__("datetime").timedelta(days=7)


def test_hibas_szuro_eseten_a_mai_honap():
    period = stats_service.parse_period("valami", "nem-datum")
    assert period.mode == "month"
    assert period.first.day == 1


def test_a_honap_neve_magyarul_jelenik_meg():
    assert stats_service.period_of(date(2026, 9, 1), "month").label == "2026. szeptember"
    assert "hét" in stats_service.period_of(date(2026, 9, 9), "week").label


# --------------------------------------------------------------------------
# Napi óraszám
# --------------------------------------------------------------------------
def test_a_napi_bontas_minden_napot_tartalmaz_a_nulla_orasat_is():
    """A hétvégék és a hiányzások látszódjanak a grafikonon, ne tűnjenek el."""
    employee_id = make_employee()
    make_session(employee_id, "2026-09-07T08:00:00+02:00", "2026-09-07T16:00:00+02:00")
    make_session(employee_id, "2026-09-09T08:00:00+02:00", "2026-09-09T12:00:00+02:00")

    period = stats_service.period_of(date(2026, 9, 9), "week")
    with SessionLocal() as db:
        bars = stats_service.daily_bars(db, period, employee_id)

    assert len(bars) == 7
    assert [bar.hours for bar in bars] == [8.0, 0.0, 4.0, 0.0, 0.0, 0.0, 0.0]
    assert [bar.local_date.isoformat() for bar in bars][0] == "2026-09-07"
    assert bars[5].is_weekend is True  # szombat
    assert bars[0].weekday == "hétfő"


def test_egy_napon_tobb_munkamenet_osszeadodik():
    employee_id = make_employee()
    make_session(employee_id, "2026-09-09T08:00:00+02:00", "2026-09-09T12:00:00+02:00")
    make_session(employee_id, "2026-09-09T13:00:00+02:00", "2026-09-09T17:00:00+02:00")

    period = stats_service.period_of(date(2026, 9, 9), "week")
    with SessionLocal() as db:
        bars = stats_service.daily_bars(db, period, employee_id)

    szerda = next(bar for bar in bars if bar.local_date == date(2026, 9, 9))
    assert szerda.hours == 8.0
    assert szerda.sessions == 2


def test_az_automatikusan_zart_nap_meg_van_jelolve():
    employee_id = make_employee()
    make_session(
        employee_id, "2026-09-09T08:00:00+02:00", "2026-09-09T23:59:00+02:00", auto_closed=True
    )

    period = stats_service.period_of(date(2026, 9, 9), "month")
    with SessionLocal() as db:
        bars = stats_service.daily_bars(db, period, employee_id)

    szerda = next(bar for bar in bars if bar.local_date == date(2026, 9, 9))
    assert szerda.auto_closed is True
    assert szerda.hours == 15.98


def test_a_napi_bontas_csak_a_valasztott_dolgozot_mutatja():
    elso = make_employee(name="Első Elek", code="E001", uid="AAAA1111")
    masodik = make_employee(name="Második Mária", code="E002", uid="BBBB2222")
    make_session(elso, "2026-09-09T08:00:00+02:00", "2026-09-09T16:00:00+02:00")
    make_session(masodik, "2026-09-09T08:00:00+02:00", "2026-09-09T12:00:00+02:00")

    period = stats_service.period_of(date(2026, 9, 9), "week")
    with SessionLocal() as db:
        bars = stats_service.daily_bars(db, period, elso)

    assert sum(bar.hours for bar in bars) == 8.0


def test_az_ejfelen_atnyulo_muszak_a_kezdes_napjahoz_kerul():
    employee_id = make_employee()
    make_session(employee_id, "2026-09-09T22:00:00+02:00", "2026-09-10T06:00:00+02:00")

    period = stats_service.period_of(date(2026, 9, 9), "week")
    with SessionLocal() as db:
        bars = stats_service.daily_bars(db, period, employee_id)

    assert next(bar for bar in bars if bar.local_date == date(2026, 9, 9)).hours == 8.0
    assert next(bar for bar in bars if bar.local_date == date(2026, 9, 10)).hours == 0.0


def test_dst_napon_a_valodi_ora_jelenik_meg():
    employee_id = make_employee()
    make_session(employee_id, "2026-03-29T01:30:00+01:00", "2026-03-29T05:30:00+02:00")

    period = stats_service.period_of(date(2026, 3, 29), "month")
    with SessionLocal() as db:
        bars = stats_service.daily_bars(db, period, employee_id)

    assert next(bar for bar in bars if bar.local_date == date(2026, 3, 29)).hours == 3.0


# --------------------------------------------------------------------------
# Dolgozók összehasonlítása
# --------------------------------------------------------------------------
def test_az_osszehasonlitas_csokkeno_sorrendben_jon():
    kevés = make_employee(name="Kevés Károly", code="E001", uid="AAAA1111")
    sok = make_employee(name="Sok Sándor", code="E002", uid="BBBB2222")
    kozepes = make_employee(name="Közepes Kata", code="E003", uid="CCCC3333")

    make_session(kevés, "2026-09-01T08:00:00+02:00", "2026-09-01T12:00:00+02:00")  # 4
    make_session(sok, "2026-09-01T08:00:00+02:00", "2026-09-01T20:00:00+02:00")  # 12
    make_session(kozepes, "2026-09-01T08:00:00+02:00", "2026-09-01T16:00:00+02:00")  # 8

    period = stats_service.period_of(date(2026, 9, 1), "month")
    with SessionLocal() as db:
        totals = stats_service.employee_totals(db, period)

    assert [total.name for total in totals] == ["Sok Sándor", "Közepes Kata", "Kevés Károly"]
    assert [total.hours for total in totals] == [12.0, 8.0, 4.0]


def test_az_osszehasonlitas_napszamot_es_atlagot_is_ad():
    employee_id = make_employee()
    make_session(employee_id, "2026-09-01T08:00:00+02:00", "2026-09-01T16:00:00+02:00")
    make_session(employee_id, "2026-09-02T08:00:00+02:00", "2026-09-02T12:00:00+02:00")
    make_session(employee_id, "2026-09-02T13:00:00+02:00", "2026-09-02T17:00:00+02:00")

    period = stats_service.period_of(date(2026, 9, 1), "month")
    with SessionLocal() as db:
        total = stats_service.employee_totals(db, period)[0]

    assert total.hours == 16.0
    assert total.day_count == 2  # két NAP, nem három munkamenet
    assert total.average_hours_per_day == 8.0


def test_az_idoszakon_kivuli_adat_nem_szamit_bele():
    employee_id = make_employee()
    make_session(employee_id, "2026-08-31T08:00:00+02:00", "2026-08-31T16:00:00+02:00")
    make_session(employee_id, "2026-09-01T08:00:00+02:00", "2026-09-01T12:00:00+02:00")

    period = stats_service.period_of(date(2026, 9, 15), "month")
    with SessionLocal() as db:
        totals = stats_service.employee_totals(db, period)

    assert totals[0].hours == 4.0


def test_az_ellenorzendo_napok_kulon_szamolodnak():
    employee_id = make_employee()
    make_session(employee_id, "2026-09-01T08:00:00+02:00", "2026-09-01T16:00:00+02:00")
    make_session(
        employee_id, "2026-09-02T08:00:00+02:00", "2026-09-02T23:59:00+02:00", auto_closed=True
    )

    period = stats_service.period_of(date(2026, 9, 1), "month")
    with SessionLocal() as db:
        total = stats_service.employee_totals(db, period)[0]

    assert total.day_count == 2
    assert total.auto_closed_day_count == 1


# --------------------------------------------------------------------------
# Végpontok és felület
# --------------------------------------------------------------------------
def test_a_stats_oldal_renderel(admin):
    employee_id = make_employee()
    make_session(employee_id, "2026-09-09T08:00:00+02:00", "2026-09-09T16:00:00+02:00")

    oldal = admin.get(f"/stats?mode=month&anchor=2026-09-09&employee_id={employee_id}")
    assert oldal.status_code == 200
    assert "Napi óraszám" in oldal.text
    assert "Ki mennyit volt bent" in oldal.text
    assert "2026. szeptember" in oldal.text
    # Táblázatos nézet a grafikon mellé (akadálymentesség).
    assert "Táblázatos nézet" in oldal.text
    # Jelmagyarázat: a státuszszín soha nem áll önmagában.
    assert "automatikus zárás – ellenőrzendő" in oldal.text


def test_a_stats_oldal_belepes_nelkul_atiranyit(client):
    valasz = client.get("/stats", follow_redirects=False)
    assert valasz.status_code == 303
    assert valasz.headers["location"].startswith("/login")


def test_napi_vegpont(admin):
    employee_id = make_employee()
    make_session(employee_id, "2026-09-09T08:00:00+02:00", "2026-09-09T16:00:00+02:00")

    valasz = admin.get(
        f"/api/v1/stats/daily?employee_id={employee_id}&mode=week&anchor=2026-09-09"
    )
    assert valasz.status_code == 200
    adat = valasz.json()
    assert adat["employee_name"] == "Teszt Elek"
    assert adat["total_hours"] == 8.0
    assert len(adat["bars"]) == 7
    szerda = next(bar for bar in adat["bars"] if bar["date"] == "2026-09-09")
    assert szerda["hours"] == 8.0
    assert szerda["weekday"] == "szerda"
    assert szerda["weekend"] is False


def test_napi_vegpont_ismeretlen_dolgozora_404(admin):
    assert admin.get("/api/v1/stats/daily?employee_id=999").status_code == 404


def test_dolgozo_osszehasonlito_vegpont(admin):
    elso = make_employee(name="Első Elek", code="E001", uid="AAAA1111")
    masodik = make_employee(name="Második Mária", code="E002", uid="BBBB2222")
    make_session(elso, "2026-09-01T08:00:00+02:00", "2026-09-01T20:00:00+02:00")
    make_session(masodik, "2026-09-01T08:00:00+02:00", "2026-09-01T12:00:00+02:00")

    adat = admin.get("/api/v1/stats/employees?mode=month&anchor=2026-09-01").json()
    assert adat["period_label"] == "2026. szeptember"
    assert adat["total_hours"] == 16.0
    assert [sor["name"] for sor in adat["employees"]] == ["Első Elek", "Második Mária"]
    assert [sor["hours"] for sor in adat["employees"]] == [12.0, 4.0]


def test_a_stats_vegpontok_hitelesitest_igenyelnek(client):
    assert client.get("/api/v1/stats/daily?employee_id=1").status_code == 401
    assert client.get("/api/v1/stats/employees").status_code == 401


def test_a_grafikon_orai_egyeznek_a_riport_oraival(admin):
    """A grafikon és a riport ugyanabból a forrásból számol – ne térjenek el."""
    from app.services import reports as report_service

    employee_id = make_employee()
    make_session(employee_id, "2026-09-01T08:00:00+02:00", "2026-09-01T16:32:15+02:00")
    make_session(employee_id, "2026-09-02T07:15:00+02:00", "2026-09-02T15:48:30+02:00")

    period = stats_service.period_of(date(2026, 9, 1), "month")
    with SessionLocal() as db:
        bars = stats_service.daily_bars(db, period, employee_id)
        riport = report_service.collect_sessions(db, period.first, period.last, employee_id)

    assert sum(bar.seconds for bar in bars) == sum(sor.seconds for sor in riport)


def test_belyegzesbol_szamolt_grafikon(gateway):
    """Végponttól végpontig: bélyegzésből grafikon-adat."""
    employee_id = make_employee()
    gateway.events(
        [
            punch_event(UID, "2026-09-01T08:00:00+02:00"),
            punch_event(UID, "2026-09-01T16:00:00+02:00"),
        ]
    )

    period = stats_service.period_of(date(2026, 9, 1), "month")
    with SessionLocal() as db:
        bars = stats_service.daily_bars(db, period, employee_id)

    assert next(bar for bar in bars if bar.local_date == date(2026, 9, 1)).hours == 8.0
    assert parse_iso_to_utc("2026-09-01T08:00:00+02:00")  # a segéd elérhető
