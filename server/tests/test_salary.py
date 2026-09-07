"""Bérszámítás: órabér-előzmény, kerekítés, ellenőrzendő tételek, jogosultság."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import AuditLog, Employee, EmployeeRate, WorkSession
from app.services import salary as salary_service
from app.timeutil import parse_iso_to_utc, today_local, utcnow
from conftest import make_employee, punch_event

UID = "04A1B2C3"


# --------------------------------------------------------------------------
# Segédek
# --------------------------------------------------------------------------
def make_session(
    employee_id: int,
    start_local: str,
    end_local: str | None = None,
    auto_closed: bool = False,
) -> int:
    """Munkamenet közvetlen felvétele, pontosan megadott hosszal."""
    started = parse_iso_to_utc(start_local)
    ended = parse_iso_to_utc(end_local) if end_local else None
    duration = None if ended is None else int((ended - started).total_seconds())

    with SessionLocal() as db:
        session = WorkSession(
            employee_id=employee_id,
            started_at=started,
            ended_at=ended,
            duration_seconds=duration,
            auto_closed=auto_closed,
        )
        db.add(session)
        db.commit()
        return session.id


def make_session_seconds(employee_id: int, start_local: str, seconds: int) -> int:
    started = parse_iso_to_utc(start_local)
    with SessionLocal() as db:
        session = WorkSession(
            employee_id=employee_id,
            started_at=started,
            ended_at=started + timedelta(seconds=seconds),
            duration_seconds=seconds,
            auto_closed=False,
        )
        db.add(session)
        db.commit()
        return session.id


def make_rate(employee_id: int, hourly_rate: int, valid_from: str) -> int:
    with SessionLocal() as db:
        row = salary_service.add_rate(
            db,
            employee_id=employee_id,
            hourly_rate=hourly_rate,
            valid_from=date.fromisoformat(valid_from),
        )
        db.commit()
        return row.id


@pytest.fixture
def salary_disabled():
    """SALARY_ENABLED=false szimulálása futás közben.

    A beállítások a folyamat indulásakor fagynak be, ezért a fagyasztott
    dataclasst közvetlenül írjuk át – minden modul ugyanarra az objektumra
    hivatkozik.
    """
    object.__setattr__(settings, "salary_enabled", False)
    try:
        yield
    finally:
        object.__setattr__(settings, "salary_enabled", True)


# --------------------------------------------------------------------------
# Kerekítés
# --------------------------------------------------------------------------
def test_forint_egyszer_kerekit_es_felnel_felfele():
    # 7ó 32p 15mp = 27 135 mp, 1900 Ft/óra -> 14 321,25 Ft
    assert salary_service.forint(27135, 1900) == 14321

    # Ha előbb órára kerekítenénk (7,54 óra), 14 326 Ft jönne ki -- ez a hiba,
    # amit a régi CSV export elkövetett.
    assert int(round(round(27135 / 3600, 2) * 1900)) == 14326

    # Fél forintnál felfelé, nem bankári kerekítéssel.
    assert salary_service.forint(1800, 1) == 1  # 0,5 Ft -> 1
    assert salary_service.forint(0, 1900) == 0
    assert salary_service.forint(3600, 0) == 0


# --------------------------------------------------------------------------
# A napi sorok összege = a havi végösszeg
# --------------------------------------------------------------------------
def test_napi_sorok_osszege_pontosan_a_havi_vegosszeg():
    """Konkrét eset, ahol a naiv havi újraszámolás 1 Ft-tal eltérne."""
    employee_id = make_employee()
    make_rate(employee_id, 1900, "2026-01-01")

    make_session_seconds(employee_id, "2026-09-01T08:00:00+02:00", 27000)  # 7ó 30p
    make_session_seconds(employee_id, "2026-09-02T08:00:00+02:00", 29000)  # 8ó 3p 20mp
    make_session_seconds(employee_id, "2026-09-03T08:00:00+02:00", 24601)  # 6ó 50p 1mp

    with SessionLocal() as db:
        group = salary_service.employee_month(db, employee_id, "2026-09")

    assert [day.amount for day in group.days] == [14250, 15306, 12984]
    assert group.amount == 42540
    # A követelmény: a napi sorok szummája PONTOSAN a havi végösszeg.
    assert sum(day.amount for day in group.days) == group.amount

    # Ha a hónapot a nyers másodpercekből számolnánk újra, 42 539 Ft jönne ki.
    assert salary_service.forint(group.seconds, 1900) == 42539
    assert group.amount != salary_service.forint(group.seconds, 1900)


def test_osszesito_vegosszege_a_dolgozonkenti_osszegek_szummaja():
    elso = make_employee(name="Első Elek", code="E001", uid="AAAA1111")
    masodik = make_employee(name="Második Mária", code="E002", uid="BBBB2222")
    make_rate(elso, 1900, "2026-01-01")
    make_rate(masodik, 2100, "2026-01-01")

    make_session_seconds(elso, "2026-09-01T08:00:00+02:00", 27000)
    make_session_seconds(masodik, "2026-09-01T08:00:00+02:00", 29000)

    with SessionLocal() as db:
        groups = salary_service.month_summary(db, "2026-09")
        totals = salary_service.totals_of(groups)

    assert [group.employee_name for group in groups] == ["Első Elek", "Második Mária"]
    assert totals.amount == sum(group.amount for group in groups)
    assert totals.seconds == 27000 + 29000


# --------------------------------------------------------------------------
# Órabér-előzmény
# --------------------------------------------------------------------------
def test_orabér_valtozas_kozepso_datummal():
    """A változás előtti napok a régi, utániak az új órabérrel számolnak."""
    employee_id = make_employee()
    make_rate(employee_id, 1000, "2026-01-01")
    make_rate(employee_id, 1500, "2026-09-15")

    make_session(employee_id, "2026-09-10T08:00:00+02:00", "2026-09-10T16:00:00+02:00")
    make_session(employee_id, "2026-09-14T08:00:00+02:00", "2026-09-14T16:00:00+02:00")
    make_session(employee_id, "2026-09-15T08:00:00+02:00", "2026-09-15T16:00:00+02:00")
    make_session(employee_id, "2026-09-20T08:00:00+02:00", "2026-09-20T16:00:00+02:00")

    with SessionLocal() as db:
        group = salary_service.employee_month(db, employee_id, "2026-09")

    rates = {day.local_date.isoformat(): day.hourly_rate for day in group.days}
    assert rates == {
        "2026-09-10": 1000,
        "2026-09-14": 1000,
        "2026-09-15": 1500,  # az érvényesség napja már az ÚJ órabér
        "2026-09-20": 1500,
    }
    assert [day.amount for day in group.days] == [8000, 8000, 12000, 12000]
    assert group.amount == 40000
    assert group.distinct_rates == [1000, 1500]


def test_emeles_nem_irja_at_visszamenoleg_a_korabbi_honapot():
    """Ez a legfontosabb: egy márciusi emelés nem nyúlhat a februári kimutatáshoz."""
    employee_id = make_employee()
    make_rate(employee_id, 1000, "2026-01-01")
    make_session(employee_id, "2026-02-10T08:00:00+01:00", "2026-02-10T16:00:00+01:00")

    with SessionLocal() as db:
        februar_elotte = salary_service.employee_month(db, employee_id, "2026-02").amount
    assert februar_elotte == 8000

    # Márciusi emelés.
    make_rate(employee_id, 2000, "2026-03-01")

    with SessionLocal() as db:
        februar_utana = salary_service.employee_month(db, employee_id, "2026-02")
    assert februar_utana.amount == februar_elotte == 8000
    assert februar_utana.days[0].hourly_rate == 1000


def test_nincs_sajat_orabér_a_default_ervenyes():
    employee_id = make_employee()  # nincs employee_rates sora
    make_session(employee_id, "2026-09-10T08:00:00+02:00", "2026-09-10T16:00:00+02:00")

    with SessionLocal() as db:
        group = salary_service.employee_month(db, employee_id, "2026-09")

    assert group.days[0].hourly_rate == settings.hourly_rate == 1900
    assert group.days[0].rate_is_default is True
    assert group.uses_default_rate is True
    assert group.amount == 8 * 1900


def test_a_valid_from_elotti_napokra_a_default_ervenyes():
    """Ha az órabér csak hó közepétől él, az előtte lévő napokra a default marad."""
    employee_id = make_employee()
    make_rate(employee_id, 3000, "2026-09-15")

    make_session(employee_id, "2026-09-10T08:00:00+02:00", "2026-09-10T16:00:00+02:00")
    make_session(employee_id, "2026-09-20T08:00:00+02:00", "2026-09-20T16:00:00+02:00")

    with SessionLocal() as db:
        group = salary_service.employee_month(db, employee_id, "2026-09")

    assert [day.hourly_rate for day in group.days] == [1900, 3000]
    assert [day.rate_is_default for day in group.days] == [True, False]


def test_azonos_napra_felvett_ket_orabér_kozul_az_utolso_er():
    employee_id = make_employee()
    make_rate(employee_id, 1000, "2026-09-01")
    make_rate(employee_id, 1200, "2026-09-01")  # javítás ugyanarra a napra
    make_session(employee_id, "2026-09-10T08:00:00+02:00", "2026-09-10T16:00:00+02:00")

    with SessionLocal() as db:
        group = salary_service.employee_month(db, employee_id, "2026-09")

    assert group.days[0].hourly_rate == 1200


# --------------------------------------------------------------------------
# Munkamenet-szabályok
# --------------------------------------------------------------------------
def test_ejfelen_atnyulo_muszak_a_kezdes_napjahoz_tartozik():
    employee_id = make_employee()
    make_rate(employee_id, 1000, "2026-01-01")
    make_session(employee_id, "2026-09-10T22:00:00+02:00", "2026-09-11T06:00:00+02:00")

    with SessionLocal() as db:
        group = salary_service.employee_month(db, employee_id, "2026-09")

    assert len(group.days) == 1
    assert group.days[0].local_date == date(2026, 9, 10)
    assert group.days[0].seconds == 8 * 3600
    assert group.amount == 8000


def test_nyitott_munkamenet_nem_szamit_bele():
    employee_id = make_employee()
    make_rate(employee_id, 1000, "2026-01-01")
    make_session(employee_id, "2026-09-10T08:00:00+02:00", "2026-09-10T16:00:00+02:00")
    make_session(employee_id, "2026-09-11T08:00:00+02:00", None)  # még bent van

    with SessionLocal() as db:
        group = salary_service.employee_month(db, employee_id, "2026-09")

    assert len(group.days) == 1
    assert group.days[0].local_date == date(2026, 9, 10)
    assert group.seconds == 8 * 3600
    assert group.amount == 8000


def test_ma_nyitott_munkamenet_sem_szamit_bele():
    """A jelenléti riport mutatja az eddig eltelt időt, a bér nem számol vele."""
    employee_id = make_employee()
    with SessionLocal() as db:
        db.add(
            WorkSession(
                employee_id=employee_id,
                started_at=utcnow() - timedelta(hours=3),
                auto_closed=False,
            )
        )
        db.commit()

    ma = salary_service.parse_month(None, today_local())
    with SessionLocal() as db:
        group = salary_service.employee_month(db, employee_id, ma)

    assert group.days == []
    assert group.amount == 0


def test_auto_closed_beleszamit_de_kulon_is_osszesitve():
    employee_id = make_employee()
    make_rate(employee_id, 1000, "2026-01-01")

    # Ugyanazon a napon: egy rendes délelőtt és egy nyitva felejtett délután.
    make_session(employee_id, "2026-09-10T08:00:00+02:00", "2026-09-10T12:00:00+02:00")
    make_session(
        employee_id,
        "2026-09-10T13:00:00+02:00",
        "2026-09-10T23:59:00+02:00",
        auto_closed=True,
    )

    with SessionLocal() as db:
        group = salary_service.employee_month(db, employee_id, "2026-09")

    day = group.days[0]
    # A pénz nem hiányozhat: a teljes nap benne van.
    assert day.seconds == 4 * 3600 + (10 * 3600 + 59 * 60)
    assert day.amount == salary_service.forint(day.seconds, 1000)

    # De az ellenőrzendő rész CSAK az automatikusan zárt munkamenet.
    assert day.needs_check is True
    assert day.auto_closed_count == 1
    assert day.auto_closed_seconds == 10 * 3600 + 59 * 60
    assert day.auto_closed_amount == salary_service.forint(day.auto_closed_seconds, 1000)
    assert day.auto_closed_amount < day.amount

    assert group.auto_closed_days == 1
    assert group.auto_closed_hours == round((10 * 3600 + 59 * 60) / 3600, 2)


def test_auto_closed_nelkuli_honapban_nincs_ellenorzendo():
    employee_id = make_employee()
    make_session(employee_id, "2026-09-10T08:00:00+02:00", "2026-09-10T16:00:00+02:00")

    with SessionLocal() as db:
        group = salary_service.employee_month(db, employee_id, "2026-09")

    assert group.auto_closed_days == 0
    assert group.auto_closed_amount == 0
    assert group.days[0].needs_check is False


# --------------------------------------------------------------------------
# Nyári/téli időszámítás
# --------------------------------------------------------------------------
def test_dst_tavaszi_23_oras_napon_helyes_az_osszeg():
    """2026-03-29: 01:30-tól 05:30-ig a faliórán 4 óra, valójában 3."""
    employee_id = make_employee()
    make_rate(employee_id, 1000, "2026-01-01")
    make_session(employee_id, "2026-03-29T01:30:00+01:00", "2026-03-29T05:30:00+02:00")

    with SessionLocal() as db:
        group = salary_service.employee_month(db, employee_id, "2026-03")

    assert group.days[0].local_date == date(2026, 3, 29)
    assert group.seconds == 3 * 3600
    assert group.amount == 3000  # nem 4000


def test_dst_oszi_25_oras_napon_helyes_az_osszeg():
    """2025-10-26: 01:30-tól 04:30-ig a faliórán 3 óra, valójában 4."""
    employee_id = make_employee()
    make_rate(employee_id, 1000, "2025-01-01")
    make_session(employee_id, "2025-10-26T01:30:00+02:00", "2025-10-26T04:30:00+01:00")

    with SessionLocal() as db:
        group = salary_service.employee_month(db, employee_id, "2025-10")

    assert group.days[0].local_date == date(2025, 10, 26)
    assert group.seconds == 4 * 3600
    assert group.amount == 4000  # nem 3000


# --------------------------------------------------------------------------
# Végponttól végpontig: bélyegzésből bér
# --------------------------------------------------------------------------
def test_belyegzesbol_szamolt_ber(gateway):
    employee_id = make_employee()
    make_rate(employee_id, 2500, "2026-01-01")
    gateway.events(
        [
            punch_event(UID, "2026-09-01T08:00:00+02:00"),
            punch_event(UID, "2026-09-01T16:30:00+02:00"),
        ]
    )

    with SessionLocal() as db:
        group = salary_service.employee_month(db, employee_id, "2026-09")

    assert group.seconds == 8 * 3600 + 30 * 60
    assert group.amount == salary_service.forint(8 * 3600 + 30 * 60, 2500) == 21250


# --------------------------------------------------------------------------
# Felület és jogosultság
# --------------------------------------------------------------------------
def test_reports_oldalon_megjelenik_a_ber_szakasz(admin):
    employee_id = make_employee()
    make_rate(employee_id, 1900, "2026-01-01")
    make_session(employee_id, "2026-09-10T08:00:00+02:00", "2026-09-10T16:00:00+02:00")

    page = admin.get("/reports?from=2026-09-01&to=2026-09-30&month=2026-09")
    assert page.status_code == 200
    assert "Bérszámítás" in page.text
    assert "Nem helyettesíti a bérszámfejtést" in page.text
    assert "15 200 Ft" in page.text.replace(" ", " ")
    assert "Összesítő – 2026-09" in page.text


def test_reports_oldalon_a_dolgozo_napi_bontasa(admin):
    employee_id = make_employee()
    make_rate(employee_id, 1000, "2026-01-01")
    make_session(employee_id, "2026-09-10T08:00:00+02:00", "2026-09-10T16:00:00+02:00")

    page = admin.get(f"/reports?month=2026-09&employee_id={employee_id}")
    assert page.status_code == 200
    assert "Teszt Elek – 2026-09" in page.text
    assert "2026-09-10" in page.text


def test_auto_closed_sor_sargan_es_ellenorzendo_sorral(admin):
    employee_id = make_employee()
    make_rate(employee_id, 1000, "2026-01-01")
    make_session(
        employee_id, "2026-09-10T08:00:00+02:00", "2026-09-10T23:59:00+02:00", auto_closed=True
    )

    page = admin.get(f"/reports?month=2026-09&employee_id={employee_id}")
    assert "auto-closed" in page.text
    assert "Ebből ellenőrzést igényel" in page.text
    assert "check-needed" in page.text


def test_orabér_lap_felvetel_es_torles_naplozva(admin):
    employee_id = make_employee()

    page = admin.get(f"/employees/{employee_id}/rates")
    assert page.status_code == 200
    assert "alapértelmezett" in page.text

    admin.post(
        f"/employees/{employee_id}/rates",
        data={"hourly_rate": "2400", "valid_from": "2026-09-01", "note": "éves emelés"},
    )

    with SessionLocal() as db:
        rows = salary_service.rate_history(db, employee_id)
        assert len(rows) == 1
        assert rows[0].hourly_rate == 2400
        assert rows[0].valid_from == date(2026, 9, 1)
        assert rows[0].note == "éves emelés"
        rate_id = rows[0].id

        entry = db.scalar(
            select(AuditLog).where(AuditLog.entity == "employee_rate", AuditLog.action == "create")
        )
        assert entry is not None
        assert '"hourly_rate": 2400' in entry.after_json

    admin.post(f"/employees/{employee_id}/rates/{rate_id}/delete")

    with SessionLocal() as db:
        assert salary_service.rate_history(db, employee_id) == []
        entry = db.scalar(
            select(AuditLog).where(AuditLog.entity == "employee_rate", AuditLog.action == "delete")
        )
        assert entry is not None
        assert '"hourly_rate": 2400' in entry.before_json


def test_hibas_orabér_bemenet_nem_hoz_letre_sort(admin):
    employee_id = make_employee()

    admin.post(
        f"/employees/{employee_id}/rates",
        data={"hourly_rate": "kettőezer", "valid_from": "2026-09-01"},
    )
    admin.post(
        f"/employees/{employee_id}/rates",
        data={"hourly_rate": "2000", "valid_from": "nem-datum"},
    )
    admin.post(
        f"/employees/{employee_id}/rates",
        data={"hourly_rate": "-500", "valid_from": "2026-09-01"},
    )

    with SessionLocal() as db:
        assert db.scalars(select(EmployeeRate)).all() == []


def test_ber_csv_export_napi_bontas(admin):
    employee_id = make_employee()
    make_rate(employee_id, 1900, "2026-01-01")
    make_session_seconds(employee_id, "2026-09-01T08:00:00+02:00", 27135)

    response = admin.get(f"/reports/salary/export?month=2026-09&employee_id={employee_id}")
    assert response.status_code == 200
    assert response.content.startswith(b"\xef\xbb\xbf")  # utf-8-sig BOM

    text = response.content.decode("utf-8-sig")
    assert "Orabar (Ft/ora);Alapertelmezett orabar;Osszeg (Ft);Automatikusan zart" in text
    assert ";1900;nem;14321;" in text  # egyszer kerekítve, nem 14326
    assert "OSSZESEN" in text
    assert "\r\n" in text


def test_ber_csv_export_osszesito(admin):
    elso = make_employee(name="Első Elek", code="E001", uid="AAAA1111")
    masodik = make_employee(name="Második Mária", code="E002", uid="BBBB2222")
    make_rate(elso, 1000, "2026-01-01")
    make_rate(masodik, 2000, "2026-01-01")
    make_session(elso, "2026-09-10T08:00:00+02:00", "2026-09-10T16:00:00+02:00")
    make_session(masodik, "2026-09-10T08:00:00+02:00", "2026-09-10T16:00:00+02:00")

    response = admin.get("/reports/salary/export?month=2026-09")
    assert response.status_code == 200
    text = response.content.decode("utf-8-sig")
    assert "Atlagos orabar (Ft/ora)" in text
    assert "Első Elek;E001;2026-09;8,00;1000;8000" in text
    assert "Második Mária;E002;2026-09;8,00;2000;16000" in text
    assert "MINDENKI OSSZESEN;;2026-09;16,00;;24000" in text


def test_jelenleti_export_nem_tartalmaz_penzt(admin):
    employee_id = make_employee()
    make_rate(employee_id, 1900, "2026-01-01")
    make_session(employee_id, "2026-09-10T08:00:00+02:00", "2026-09-10T16:00:00+02:00")

    text = admin.get("/reports/export?from=2026-09-01&to=2026-09-30").content.decode("utf-8-sig")
    assert "Osszeg" not in text
    assert "Automatikusan zarva" in text


def test_salary_enabled_false_eseten_404(admin, salary_disabled):
    employee_id = make_employee()

    assert admin.get("/reports/salary/export?month=2026-09").status_code == 404
    assert admin.get(f"/employees/{employee_id}/rates").status_code == 404
    assert (
        admin.post(
            f"/employees/{employee_id}/rates",
            data={"hourly_rate": "2000", "valid_from": "2026-09-01"},
            follow_redirects=False,
        ).status_code
        == 404
    )
    assert (
        admin.post(f"/employees/{employee_id}/rates/1/delete", follow_redirects=False).status_code
        == 404
    )


def test_salary_enabled_false_eseten_a_feluleten_sincs_ber(admin, salary_disabled):
    employee_id = make_employee()
    make_session(employee_id, "2026-09-10T08:00:00+02:00", "2026-09-10T16:00:00+02:00")

    page = admin.get("/reports?from=2026-09-01&to=2026-09-30")
    assert page.status_code == 200
    assert "Bérszámítás" not in page.text
    assert "Ft" not in page.text

    # A dolgozók listájáról is eltűnik az Órabér gomb.
    assert "Órabér" not in admin.get("/employees").text

    # A jelenléti riport viszont változatlanul működik.
    assert "Havi összesítés" in page.text


def test_a_jelenleti_riport_valtozatlanul_mukodik(admin):
    """A bér bevezetése nem nyúlhat a meglévő jelenléti kimutatáshoz."""
    employee_id = make_employee()
    make_session(employee_id, "2026-09-10T08:00:00+02:00", "2026-09-10T16:00:00+02:00")

    page = admin.get("/reports?from=2026-09-01&to=2026-09-30")
    assert "Napi bontás" in page.text
    assert "Havi összesítés" in page.text

    with SessionLocal() as db:
        assert db.scalar(select(Employee).where(Employee.id == employee_id)) is not None
