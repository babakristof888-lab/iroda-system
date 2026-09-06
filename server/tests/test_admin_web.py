"""Admin felület: belépés, oldalak renderelése, kézi javítás, CSV export."""

from __future__ import annotations

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import AuditLog, Card, Employee, Punch, WorkSession
from conftest import ADMIN_PASSWORD, env_reading, make_employee, punch_event

UID = "04A1B2C3"


def test_belepes_nelkul_atiranyit_a_login_oldalra(client):
    for path in ("/", "/employees", "/cards", "/reports", "/environment", "/punches"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/login")


def test_hibas_jelszo_nem_leptet_be(client):
    response = client.post(
        "/login", data={"password": "rossz", "next": "/"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "err=" in response.headers["location"]
    assert client.get("/", follow_redirects=False).status_code == 303


def test_helyes_jelszoval_belepes_es_session_cookie(client):
    response = client.post(
        "/login", data={"password": ADMIN_PASSWORD, "next": "/"}, follow_redirects=False
    )
    assert response.status_code == 303

    cookie = response.headers["set-cookie"]
    assert "iroda_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie.lower() or "samesite=lax" in cookie.lower()

    assert client.get("/", follow_redirects=False).status_code == 200


def test_kilepes_utan_ujra_atiranyit(admin):
    assert admin.post("/logout", follow_redirects=False).status_code == 303
    assert admin.get("/", follow_redirects=False).status_code == 303


def test_minden_oldal_renderel(admin, gateway):
    make_employee()
    gateway.events([punch_event(UID, "2026-09-01T08:00:00+02:00")])
    gateway.env([env_reading("2026-09-01T08:00:00+02:00", "2026-09-01T09:00:00+02:00")])
    gateway.heartbeat(version="1.0.0", queue_size=0, serial_ok=True, bme_ok=True)

    expected = {
        "/": "Gateway státusz",
        "/employees": "Teszt Elek",
        "/cards": "Nyilvántartott kártyák",
        "/reports": "Havi összesítés",
        "/environment": "Óránkénti összesítők",
        "/punches": "Kézi bélyegzés felvétele",
    }
    for path, marker in expected.items():
        response = admin.get(path)
        assert response.status_code == 200, path
        assert "text/html" in response.headers["content-type"]
        assert marker in response.text, path


def test_dolgozo_felvetele_es_inaktivalasa(admin):
    admin.post("/employees/new", data={"name": "Kovács Anna", "employee_code": "E010"})

    with SessionLocal() as db:
        employee = db.scalar(select(Employee).where(Employee.employee_code == "E010"))
        assert employee is not None
        assert employee.active is True
        employee_id = employee.id

    admin.post(f"/employees/{employee_id}/toggle")
    with SessionLocal() as db:
        assert db.get(Employee, employee_id).active is False

    # A művelet bekerült az audit naplóba.
    with SessionLocal() as db:
        actions = [row.action for row in db.scalars(select(AuditLog))]
        assert "create" in actions and "toggle_active" in actions


def test_foglalt_azonosito_nem_okoz_hibat(admin):
    admin.post("/employees/new", data={"name": "Első", "employee_code": "E010"})
    response = admin.post(
        "/employees/new", data={"name": "Második", "employee_code": "E010"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "err=" in response.headers["location"]

    with SessionLocal() as db:
        assert db.scalar(select(func.count(Employee.id))) == 1


def test_ismeretlen_kartya_hozzarendelese_a_feluletrol(admin, gateway):
    gateway.events(
        [
            punch_event("CAFE0001", "2026-09-01T08:00:00+02:00"),
            punch_event("CAFE0001", "2026-09-01T16:00:00+02:00"),
        ]
    )
    employee_id = make_employee(uid=None)

    page = admin.get("/cards")
    assert "CAFE0001" in page.text
    assert "Ismeretlen kártyák" in page.text

    admin.post("/cards/assign", data={"uid": "cafe0001", "employee_id": str(employee_id)})

    with SessionLocal() as db:
        card = db.scalar(select(Card).where(Card.uid == "CAFE0001"))
        assert card.employee_id == employee_id
        assert db.scalar(select(func.count(WorkSession.id))) == 1
        directions = [p.direction for p in db.scalars(select(Punch).order_by(Punch.ts_utc))]
        assert directions == ["IN", "OUT"]


def test_kezi_belyegzes_felvetele_zarja_a_nyitott_munkamenetet(admin, gateway):
    employee_id = make_employee()
    gateway.events([punch_event(UID, "2026-09-01T08:00:00+02:00")])

    admin.post(
        "/punches/new",
        data={
            "employee_id": str(employee_id),
            "ts_local": "2026-09-01T16:30:00",
            "direction": "OUT",
        },
    )

    with SessionLocal() as db:
        sessions = list(db.scalars(select(WorkSession)))
        assert len(sessions) == 1
        assert sessions[0].duration_seconds == 8 * 3600 + 30 * 60
        assert sessions[0].auto_closed is False

        entry = db.scalar(
            select(AuditLog).where(AuditLog.entity == "punch", AuditLog.action == "create")
        )
        assert entry is not None
        assert entry.after_json and "manual" in entry.after_json


def test_irany_felulirasa_naplozodik_es_ujraszamol(admin, gateway):
    make_employee()
    gateway.events(
        [
            punch_event(UID, "2026-09-01T08:00:00+02:00"),
            punch_event(UID, "2026-09-01T16:00:00+02:00"),
        ]
    )

    with SessionLocal() as db:
        second = db.scalars(select(Punch).order_by(Punch.ts_utc)).all()[1]
        punch_id = second.id

    admin.post(f"/punches/{punch_id}/direction", data={"direction": "IN"})

    with SessionLocal() as db:
        punch = db.get(Punch, punch_id)
        assert punch.direction == "IN"
        assert "manual_fix" in punch.note

        # Az újraszámolás a kézi irányt tiszteletben tartja: két külön munkamenet.
        sessions = list(db.scalars(select(WorkSession).order_by(WorkSession.started_at)))
        assert len(sessions) == 2
        assert sessions[0].auto_closed is True

        entry = db.scalar(select(AuditLog).where(AuditLog.action == "override_direction"))
        assert entry is not None
        assert entry.before_json and '"direction": "OUT"' in entry.before_json
        assert entry.after_json and '"direction": "IN"' in entry.after_json


def test_belyegzes_torlese_naplozodik_es_ujraszamol(admin, gateway):
    make_employee()
    gateway.events(
        [
            punch_event(UID, "2026-09-01T08:00:00+02:00"),
            punch_event(UID, "2026-09-01T12:00:00+02:00"),
            punch_event(UID, "2026-09-01T16:00:00+02:00"),
        ]
    )

    with SessionLocal() as db:
        middle = db.scalars(select(Punch).order_by(Punch.ts_utc)).all()[1]
        punch_id = middle.id

    admin.post(f"/punches/{punch_id}/delete")

    with SessionLocal() as db:
        assert db.get(Punch, punch_id) is None
        sessions = list(db.scalars(select(WorkSession)))
        assert len(sessions) == 1
        assert sessions[0].duration_seconds == 8 * 3600
        assert db.scalar(select(AuditLog).where(AuditLog.action == "delete")) is not None


def test_riport_csv_export_bom_es_pontosvesszo(admin, gateway):
    make_employee()
    gateway.events(
        [
            punch_event(UID, "2026-09-01T08:00:00+02:00"),
            punch_event(UID, "2026-09-01T16:00:00+02:00"),
        ]
    )

    response = admin.get("/reports/export?from=2026-09-01&to=2026-09-01")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]

    raw = response.content
    assert raw.startswith(b"\xef\xbb\xbf")  # utf-8-sig BOM

    text = raw.decode("utf-8-sig")
    lines = text.strip().split("\r\n")
    assert lines[0].startswith("Dolgozo;Azonosito;Datum")
    assert "Teszt Elek;E001;2026-09-01;08:00:00;16:00:00;8,00;nem;nem" in lines[1]


def test_kornyezet_csv_export(admin, gateway):
    gateway.env([env_reading("2026-09-01T08:00:00+02:00", "2026-09-01T09:00:00+02:00")])
    response = admin.get("/environment/export?hours=720")

    assert response.status_code == 200
    assert response.content.startswith(b"\xef\xbb\xbf")
    text = response.content.decode("utf-8-sig")
    assert "Idoszak kezdete;Idoszak vege" in text
    assert "23,41" in text  # magyar tizedesvessző


def test_riport_szurese_dolgozora(admin, gateway):
    make_employee(name="Első Elek", code="E001", uid="AAAA1111")
    make_employee(name="Második Mária", code="E002", uid="BBBB2222")
    gateway.events(
        [
            punch_event("AAAA1111", "2026-09-01T08:00:00+02:00"),
            punch_event("AAAA1111", "2026-09-01T16:00:00+02:00"),
            punch_event("BBBB2222", "2026-09-01T09:00:00+02:00"),
            punch_event("BBBB2222", "2026-09-01T17:00:00+02:00"),
        ]
    )

    with SessionLocal() as db:
        elso = db.scalar(select(Employee).where(Employee.employee_code == "E001"))
        elso_id = elso.id

    page = admin.get(f"/reports?from=2026-09-01&to=2026-09-01&employee_id={elso_id}")
    assert "Első Elek" in page.text
    assert "Második Mária" not in page.text.split("<tbody>")[1]


def test_automatikusan_zart_sor_sargan_jelenik_meg(admin, gateway):
    make_employee()
    gateway.events([punch_event(UID, "2026-09-01T08:00:00+02:00")])

    page = admin.get("/reports?from=2026-09-01&to=2026-09-01")
    assert "auto-closed" in page.text
    assert "automatikus zárás" in page.text
