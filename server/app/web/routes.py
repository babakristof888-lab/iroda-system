"""Admin HTML felület – Jinja2 templatek, vanilla JS, semmi build step."""

from __future__ import annotations

import csv
import io
import logging
import os
import uuid
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import Card, Employee, EmployeeRate, Punch
from ..security import (
    clear_session,
    issue_session,
    require_admin,
    verify_password,
)
from ..services import audit
from ..services import env_readings as env_service
from ..services import gateways as gateway_service
from ..services import punches as punch_service
from ..services import reports as report_service
from ..services import salary as salary_service
from ..timeutil import (
    fmt_date,
    fmt_dt,
    fmt_hours,
    fmt_time,
    hours_of,
    local_range_bounds_utc,
    parse_iso_to_utc,
    today_local,
    utcnow,
)

log = logging.getLogger("iroda.web")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
templates.env.filters["dt"] = fmt_dt
templates.env.filters["time"] = fmt_time
templates.env.filters["date"] = fmt_date
templates.env.filters["hours"] = fmt_hours
templates.env.filters["hnum"] = hours_of
templates.env.globals["settings"] = settings
# A "ft" szűrő regisztrációja a _ft definíciója után, a fájl alján történik.

router = APIRouter(tags=["admin"])

DIRECTION_LABELS = {"IN": "Belépés", "OUT": "Kilépés", None: "—"}


def require_salary_enabled() -> None:
    """A bérdata érzékenyebb, mint a jelenléti adat.

    SALARY_ENABLED=false esetén a bérrel kapcsolatos útvonalak úgy
    viselkednek, mintha nem is léteznének.
    """
    if not settings.salary_enabled:
        raise HTTPException(status_code=404, detail="Not Found")


def render(request: Request, name: str, context: dict) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"now": utcnow(), **context})


def _parse_date(raw: str | None, fallback: date) -> date:
    if not raw:
        return fallback
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return fallback


def _parse_local_input(raw: str) -> tuple[datetime, str]:
    """A `datetime-local` mező értéke -> (naiv UTC, nyers string)."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("Hiányzó időpont")
    return parse_iso_to_utc(text), text


def _back(url: str, message: str | None = None, error: str | None = None) -> RedirectResponse:
    params = []
    if message:
        params.append(f"msg={message}")
    if error:
        params.append(f"err={error}")
    if params:
        url = f"{url}{'&' if '?' in url else '?'}{'&'.join(params)}"
    return RedirectResponse(url, status_code=303)


# --------------------------------------------------------------------------
# Belépés
# --------------------------------------------------------------------------
@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/", err: str | None = None) -> HTMLResponse:
    return render(
        request,
        "login.html",
        {
            "next_url": next,
            "error": err,
            "configured": bool(settings.admin_password_hash),
        },
    )


@router.post("/login")
def login_submit(
    request: Request, password: str = Form(default=""), next: str = Form(default="/")
) -> RedirectResponse:
    if not verify_password(password):
        return RedirectResponse("/login?err=Hibás+jelszó", status_code=303)
    target = next if next.startswith("/") else "/"
    response = RedirectResponse(target, status_code=303)
    issue_session(response)
    return response


@router.post("/logout")
def logout() -> RedirectResponse:
    response = RedirectResponse("/login", status_code=303)
    clear_session(response)
    return response


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
) -> HTMLResponse:
    today = today_local()
    latest = env_service.latest_reading(db)
    return render(
        request,
        "dashboard.html",
        {
            "inside": report_service.who_is_in(db),
            "gateways": gateway_service.status_list(db),
            "today_punches": report_service.punches_of_day(db, today),
            "today": today,
            "latest_env": latest,
            "env_age_minutes": None
            if latest is None
            else int((utcnow() - latest.period_end_utc).total_seconds() // 60),
            "env_alerts": env_service.evaluate_alerts(latest),
            "unknown_count": len(punch_service.unknown_card_uids(db)),
            "direction_labels": DIRECTION_LABELS,
        },
    )


# --------------------------------------------------------------------------
# Dolgozók
# --------------------------------------------------------------------------
@router.get("/employees", response_class=HTMLResponse)
def employees_page(
    request: Request,
    msg: str | None = None,
    err: str | None = None,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
) -> HTMLResponse:
    rows = db.execute(
        select(Employee, func.count(Card.id))
        .join(Card, Card.employee_id == Employee.id, isouter=True)
        .group_by(Employee.id)
        .order_by(Employee.active.desc(), Employee.name)
    ).all()
    return render(
        request,
        "employees.html",
        {"employees": rows, "message": msg, "error": err},
    )


@router.post("/employees/new")
def employee_create(
    name: str = Form(...),
    employee_code: str = Form(...),
    db: Session = Depends(get_db),
    actor: str = Depends(require_admin),
) -> RedirectResponse:
    name, employee_code = name.strip(), employee_code.strip()
    if not name or not employee_code:
        return _back("/employees", error="A név és az azonosító kötelező")

    employee = Employee(name=name, employee_code=employee_code, active=True)
    db.add(employee)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return _back("/employees", error=f"Ez az azonosító már foglalt: {employee_code}")

    audit.record(
        db,
        "create",
        "employee",
        employee.id,
        after={"name": name, "employee_code": employee_code, "active": True},
        actor=actor,
    )
    db.commit()
    return _back("/employees", message=f"{name} felvéve")


@router.post("/employees/{employee_id}/edit")
def employee_edit(
    employee_id: int,
    name: str = Form(...),
    employee_code: str = Form(...),
    db: Session = Depends(get_db),
    actor: str = Depends(require_admin),
) -> RedirectResponse:
    employee = db.get(Employee, employee_id)
    if employee is None:
        return _back("/employees", error="Nincs ilyen dolgozó")

    before = {"name": employee.name, "employee_code": employee.employee_code}
    employee.name = name.strip() or employee.name
    employee.employee_code = employee_code.strip() or employee.employee_code
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return _back("/employees", error="Ez az azonosító már foglalt")

    audit.record(
        db,
        "update",
        "employee",
        employee.id,
        before=before,
        after={"name": employee.name, "employee_code": employee.employee_code},
        actor=actor,
    )
    db.commit()
    return _back("/employees", message="Módosítva")


@router.post("/employees/{employee_id}/toggle")
def employee_toggle(
    employee_id: int,
    db: Session = Depends(get_db),
    actor: str = Depends(require_admin),
) -> RedirectResponse:
    employee = db.get(Employee, employee_id)
    if employee is None:
        return _back("/employees", error="Nincs ilyen dolgozó")

    before = {"active": employee.active}
    employee.active = not employee.active
    audit.record(
        db,
        "toggle_active",
        "employee",
        employee.id,
        before=before,
        after={"active": employee.active},
        actor=actor,
    )
    db.commit()
    state = "aktív" if employee.active else "inaktív"
    return _back("/employees", message=f"{employee.name} mostantól {state}")


# --------------------------------------------------------------------------
# Kártyák
# --------------------------------------------------------------------------
@router.get("/cards", response_class=HTMLResponse)
def cards_page(
    request: Request,
    msg: str | None = None,
    err: str | None = None,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
) -> HTMLResponse:
    cards = db.execute(
        select(Card, Employee)
        .join(Employee, Employee.id == Card.employee_id, isouter=True)
        .order_by(Card.uid)
    ).all()
    employees = list(
        db.scalars(select(Employee).where(Employee.active.is_(True)).order_by(Employee.name))
    )
    return render(
        request,
        "cards.html",
        {
            "cards": cards,
            "employees": employees,
            "unknown": punch_service.unknown_card_uids(db),
            "message": msg,
            "error": err,
        },
    )


@router.post("/cards/assign")
def card_assign(
    uid: str = Form(...),
    employee_id: str = Form(default=""),
    note: str = Form(default=""),
    db: Session = Depends(get_db),
    actor: str = Depends(require_admin),
) -> RedirectResponse:
    uid = punch_service.normalize_uid(uid)
    if not uid:
        return _back("/cards", error="Hiányzó UID")

    target_id: int | None = None
    if employee_id.strip():
        try:
            target_id = int(employee_id)
        except ValueError:
            return _back("/cards", error="Érvénytelen dolgozó")
        if db.get(Employee, target_id) is None:
            return _back("/cards", error="Nincs ilyen dolgozó")

    existing = db.scalar(select(Card).where(Card.uid == uid))
    before = (
        None
        if existing is None
        else {"uid": existing.uid, "employee_id": existing.employee_id, "active": existing.active}
    )

    card, backfilled = punch_service.assign_card(db, uid, target_id, note.strip() or None)
    audit.record(
        db,
        "assign_card",
        "card",
        card.id,
        before=before,
        after={"uid": card.uid, "employee_id": card.employee_id, "backfilled_punches": backfilled},
        actor=actor,
    )
    db.commit()

    if target_id is None:
        return _back("/cards", message=f"{uid} hozzárendelése törölve")
    if backfilled:
        return _back(
            "/cards",
            message=f"{uid} hozzárendelve, {backfilled} korábbi bélyegzés visszamenőleg feldolgozva",
        )
    return _back("/cards", message=f"{uid} hozzárendelve")


@router.post("/cards/{card_id}/toggle")
def card_toggle(
    card_id: int,
    db: Session = Depends(get_db),
    actor: str = Depends(require_admin),
) -> RedirectResponse:
    card = db.get(Card, card_id)
    if card is None:
        return _back("/cards", error="Nincs ilyen kártya")

    before = {"active": card.active}
    card.active = not card.active
    audit.record(
        db, "toggle_active", "card", card.id, before=before, after={"active": card.active}, actor=actor
    )
    db.commit()
    state = "aktív" if card.active else "letiltva"
    return _back("/cards", message=f"{card.uid} mostantól {state}")


# --------------------------------------------------------------------------
# Riportok
# --------------------------------------------------------------------------
def _report_filters(
    date_from: str, date_to: str, employee_id: str
) -> tuple[date, date, int | None]:
    today = today_local()
    default_first = today.replace(day=1)
    first = _parse_date(date_from, default_first)
    last = _parse_date(date_to, today)
    if last < first:
        first, last = last, first
    selected: int | None = None
    if employee_id.strip():
        try:
            selected = int(employee_id)
        except ValueError:
            selected = None
    return first, last, selected


@router.get("/reports", response_class=HTMLResponse)
def reports_page(
    request: Request,
    date_from: str = Query(default="", alias="from"),
    date_to: str = Query(default="", alias="to"),
    employee_id: str = Query(default=""),
    month: str = Query(default=""),
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
) -> HTMLResponse:
    first, last, selected = _report_filters(date_from, date_to, employee_id)
    rows = report_service.collect_sessions(db, first, last, selected)
    employees = list(db.scalars(select(Employee).order_by(Employee.name)))

    context = {
        "employees": employees,
        "selected_employee": selected,
        "date_from": first.isoformat(),
        "date_to": last.isoformat(),
        "days": report_service.group_daily(rows),
        "months": report_service.group_monthly(rows),
        "total_seconds": sum(r.seconds for r in rows),
        "salary_enabled": settings.salary_enabled,
    }

    if settings.salary_enabled:
        # A bér-szakasz mindig egy egész hónapra vonatkozik, függetlenül a
        # jelenléti riport dátumtartományától.
        salary_month = salary_service.parse_month(month, last)
        summary = salary_service.month_summary(db, salary_month)
        context.update(
            salary_month=salary_month,
            salary_summary=summary,
            salary_totals=salary_service.totals_of(summary),
            salary_employee=(
                salary_service.employee_month(db, selected, salary_month)
                if selected is not None
                else None
            ),
        )

    return render(request, "reports.html", context)


@router.get("/reports/export")
def reports_export(
    date_from: str = Query(default="", alias="from"),
    date_to: str = Query(default="", alias="to"),
    employee_id: str = Query(default=""),
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
) -> Response:
    first, last, selected = _report_filters(date_from, date_to, employee_id)
    rows = report_service.collect_sessions(db, first, last, selected)

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    # Ez a jelenléti kimutatás: szándékosan nincs benne pénz. Munkamenetenként
    # szorozni óradíjat kerekítési hibát vinne bele, a bér pedig napi
    # összesítésből számol -- azt a /reports/salary/export adja.
    writer.writerow(
        [
            "Dolgozo",
            "Azonosito",
            "Datum",
            "Belepes",
            "Kilepes",
            "Ledolgozott ora",
            "Automatikusan zarva",
            "Meg nyitva",
        ]
    )

    for row in sorted(rows, key=lambda r: (r.employee_name, r.started_at)):
        writer.writerow(
            [
                row.employee_name,
                row.employee_code,
                row.local_date.isoformat(),
                fmt_time(row.started_at),
                fmt_time(row.ended_at) if row.ended_at else "",
                f"{row.hours:.2f}".replace(".", ","),
                "igen" if row.auto_closed else "nem",
                "igen" if row.is_open else "nem",
            ]
        )

    # utf-8-sig: az Excel BOM nélkül elrontaná az ékezeteket.
    payload = buffer.getvalue().encode("utf-8-sig")
    filename = f"munkaido_{first.isoformat()}_{last.isoformat()}.csv"
    return Response(
        content=payload,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --------------------------------------------------------------------------
# Bérszámítás
# --------------------------------------------------------------------------
def _ft(value: int) -> str:
    """Ezres tagolás nem törő szóközzel, magyar szokás szerint."""
    return f"{value:,}".replace(",", "\u00a0")


@router.get("/reports/salary/export")
def salary_export(
    month: str = Query(default=""),
    employee_id: str = Query(default=""),
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
    __: None = Depends(require_salary_enabled),
) -> Response:
    """Bér CSV. Dolgozóval napi bontás, dolgozó nélkül a havi összesítő."""
    salary_month = salary_service.parse_month(month, today_local())

    selected: int | None = None
    if employee_id.strip():
        try:
            selected = int(employee_id)
        except ValueError:
            selected = None

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")

    def hours_cell(hours: float) -> str:
        return f"{hours:.2f}".replace(".", ",")

    if selected is not None:
        group = salary_service.employee_month(db, selected, salary_month)
        writer.writerow(
            [
                "Dolgozo",
                "Azonosito",
                "Datum",
                "Elso belepes",
                "Utolso kilepes",
                "Ledolgozott ora",
                "Orabar (Ft/ora)",
                "Alapertelmezett orabar",
                "Osszeg (Ft)",
                "Automatikusan zart",
                "Ellenorzendo ora",
                "Ellenorzendo osszeg (Ft)",
            ]
        )
        for day in group.days:
            writer.writerow(
                [
                    group.employee_name,
                    group.employee_code,
                    day.local_date.isoformat(),
                    fmt_time(day.first_in),
                    fmt_time(day.last_out) if day.last_out else "",
                    hours_cell(day.hours),
                    day.hourly_rate,
                    "igen" if day.rate_is_default else "nem",
                    day.amount,
                    "igen" if day.needs_check else "nem",
                    hours_cell(day.auto_closed_hours) if day.needs_check else "",
                    day.auto_closed_amount if day.needs_check else "",
                ]
            )
        writer.writerow([])
        writer.writerow(
            [
                "OSSZESEN",
                "",
                salary_month,
                "",
                "",
                hours_cell(group.hours),
                "",
                "",
                group.amount,
                group.auto_closed_days,
                hours_cell(group.auto_closed_hours),
                group.auto_closed_amount,
            ]
        )
        filename = f"ber_{group.employee_code or group.employee_id}_{salary_month}.csv"
    else:
        groups = salary_service.month_summary(db, salary_month)
        totals = salary_service.totals_of(groups)
        writer.writerow(
            [
                "Dolgozo",
                "Azonosito",
                "Honap",
                "Ledolgozott ora",
                "Atlagos orabar (Ft/ora)",
                "Osszeg (Ft)",
                "Ellenorzendo nap",
                "Ellenorzendo ora",
                "Ellenorzendo osszeg (Ft)",
            ]
        )
        for group in groups:
            writer.writerow(
                [
                    group.employee_name,
                    group.employee_code,
                    group.month,
                    hours_cell(group.hours),
                    group.average_rate,
                    group.amount,
                    group.auto_closed_days,
                    hours_cell(group.auto_closed_hours),
                    group.auto_closed_amount,
                ]
            )
        writer.writerow([])
        writer.writerow(
            [
                "MINDENKI OSSZESEN",
                "",
                salary_month,
                hours_cell(totals.hours),
                "",
                totals.amount,
                totals.auto_closed_days,
                hours_cell(totals.auto_closed_hours),
                totals.auto_closed_amount,
            ]
        )
        filename = f"ber_osszesito_{salary_month}.csv"

    payload = buffer.getvalue().encode("utf-8-sig")
    return Response(
        content=payload,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/employees/{employee_id}/rates", response_class=HTMLResponse)
def employee_rates_page(
    request: Request,
    employee_id: int,
    msg: str | None = None,
    err: str | None = None,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
    __: None = Depends(require_salary_enabled),
) -> HTMLResponse:
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Nincs ilyen dolgozó")

    rate, is_default = salary_service.current_rate(db, employee_id, today_local())
    return render(
        request,
        "employee_rates.html",
        {
            "employee": employee,
            "rates": salary_service.rate_history(db, employee_id),
            "current_rate": rate,
            "current_is_default": is_default,
            "today": today_local().isoformat(),
            "message": msg,
            "error": err,
        },
    )


@router.post("/employees/{employee_id}/rates")
def employee_rate_add(
    employee_id: int,
    hourly_rate: str = Form(...),
    valid_from: str = Form(...),
    note: str = Form(default=""),
    db: Session = Depends(get_db),
    actor: str = Depends(require_admin),
    __: None = Depends(require_salary_enabled),
) -> RedirectResponse:
    back = f"/employees/{employee_id}/rates"
    if db.get(Employee, employee_id) is None:
        raise HTTPException(status_code=404, detail="Nincs ilyen dolgozó")

    try:
        rate_value = int(str(hourly_rate).strip().replace(" ", "").replace("\u00a0", ""))
    except ValueError:
        return _back(back, error="Az órabér csak egész szám lehet")
    if rate_value < 0:
        return _back(back, error="Az órabér nem lehet negatív")

    try:
        valid_date = date.fromisoformat(valid_from.strip())
    except ValueError:
        return _back(back, error="Érvénytelen kezdő dátum")

    row = salary_service.add_rate(
        db,
        employee_id=employee_id,
        hourly_rate=rate_value,
        valid_from=valid_date,
        created_by=actor,
        note=note.strip() or None,
    )
    audit.record(
        db,
        "create",
        "employee_rate",
        row.id,
        after={
            "employee_id": employee_id,
            "hourly_rate": rate_value,
            "valid_from": valid_date,
            "note": row.note,
        },
        actor=actor,
    )
    db.commit()
    return _back(back, message=f"{_ft(rate_value)} Ft/óra érvényes {valid_date.isoformat()}-tól")


@router.post("/employees/{employee_id}/rates/{rate_id}/delete")
def employee_rate_delete(
    employee_id: int,
    rate_id: int,
    db: Session = Depends(get_db),
    actor: str = Depends(require_admin),
    __: None = Depends(require_salary_enabled),
) -> RedirectResponse:
    back = f"/employees/{employee_id}/rates"
    row = db.get(EmployeeRate, rate_id)
    if row is None or row.employee_id != employee_id:
        return _back(back, error="Nincs ilyen órabér-sor")

    before = {
        "employee_id": row.employee_id,
        "hourly_rate": row.hourly_rate,
        "valid_from": row.valid_from,
        "note": row.note,
    }
    db.delete(row)
    db.flush()
    audit.record(db, "delete", "employee_rate", rate_id, before=before, actor=actor)
    db.commit()
    return _back(back, message="Órabér-sor törölve")


# --------------------------------------------------------------------------
# Környezet
# --------------------------------------------------------------------------
ALLOWED_RANGES = {24: "24 óra", 24 * 7: "7 nap", 24 * 30: "30 nap"}


@router.get("/environment", response_class=HTMLResponse)
def environment_page(
    request: Request,
    hours: int = Query(default=24),
    sensor_id: str = Query(default="default"),
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
) -> HTMLResponse:
    if hours not in ALLOWED_RANGES:
        hours = 24
    rows = env_service.readings_since(db, hours, sensor_id)
    latest = env_service.latest_reading(db, sensor_id)
    return render(
        request,
        "environment.html",
        {
            "hours": hours,
            "ranges": ALLOWED_RANGES,
            "sensor_id": sensor_id,
            "sensors": env_service.known_sensor_ids(db) or ["default"],
            "readings": list(reversed(rows)),
            "latest": latest,
            "alerts": env_service.evaluate_alerts(latest),
        },
    )


@router.get("/environment/export")
def environment_export(
    hours: int = Query(default=24),
    sensor_id: str = Query(default="default"),
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
) -> Response:
    if hours not in ALLOWED_RANGES:
        hours = 24
    rows = env_service.readings_since(db, hours, sensor_id)

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(
        [
            "Idoszak kezdete",
            "Idoszak vege",
            "Szenzor",
            "Mintaszam",
            "Hom. atlag",
            "Hom. min",
            "Hom. max",
            "Para atlag",
            "Para min",
            "Para max",
            "Legnyomas atlag",
        ]
    )

    def num(value: float | None) -> str:
        return "" if value is None else f"{value:.2f}".replace(".", ",")

    for row in rows:
        writer.writerow(
            [
                fmt_dt(row.period_start_utc),
                fmt_dt(row.period_end_utc),
                row.sensor_id,
                row.sample_count,
                num(row.temp_avg),
                num(row.temp_min),
                num(row.temp_max),
                num(row.hum_avg),
                num(row.hum_min),
                num(row.hum_max),
                num(row.press_avg),
            ]
        )

    payload = buffer.getvalue().encode("utf-8-sig")
    filename = f"kornyezet_{hours}h_{today_local().isoformat()}.csv"
    return Response(
        content=payload,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --------------------------------------------------------------------------
# Nyers eseménynapló és kézi javítás
# --------------------------------------------------------------------------
@router.get("/punches", response_class=HTMLResponse)
def punches_page(
    request: Request,
    date_from: str = Query(default="", alias="from"),
    date_to: str = Query(default="", alias="to"),
    employee_id: str = Query(default=""),
    uid: str = Query(default=""),
    msg: str | None = None,
    err: str | None = None,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin),
) -> HTMLResponse:
    today = today_local()
    first = _parse_date(date_from, today - timedelta(days=7))
    last = _parse_date(date_to, today)
    if last < first:
        first, last = last, first

    start_utc, end_utc = local_range_bounds_utc(first, last)
    query = (
        select(Punch, Employee)
        .join(Employee, Employee.id == Punch.employee_id, isouter=True)
        .where(Punch.ts_utc >= start_utc, Punch.ts_utc < end_utc)
        .order_by(Punch.ts_utc.desc(), Punch.id.desc())
        .limit(1000)
    )
    if employee_id.strip():
        if employee_id == "none":
            query = query.where(Punch.employee_id.is_(None))
        else:
            try:
                query = query.where(Punch.employee_id == int(employee_id))
            except ValueError:
                pass
    if uid.strip():
        needle = f"%{punch_service.normalize_uid(uid)}%"
        query = query.where(Punch.card_uid.like(needle))

    return render(
        request,
        "punches.html",
        {
            "rows": db.execute(query).all(),
            "employees": list(db.scalars(select(Employee).order_by(Employee.name))),
            "selected_employee": employee_id,
            "date_from": first.isoformat(),
            "date_to": last.isoformat(),
            "uid": uid,
            "message": msg,
            "error": err,
            "direction_labels": DIRECTION_LABELS,
            "audit_entries": audit.recent(db, limit=25),
        },
    )


@router.post("/punches/new")
def punch_create(
    employee_id: int = Form(...),
    ts_local: str = Form(...),
    direction: str = Form(default=""),
    db: Session = Depends(get_db),
    actor: str = Depends(require_admin),
) -> RedirectResponse:
    try:
        ts_utc, raw = _parse_local_input(ts_local)
    except ValueError:
        return _back("/punches", error="Érvénytelen időpont")

    try:
        punch = punch_service.create_manual_punch(
            db,
            employee_id=employee_id,
            ts_utc=ts_utc,
            direction=direction if direction in ("IN", "OUT") else None,
            ts_local_raw=raw,
            event_uuid=f"manual-{uuid.uuid4()}",
        )
    except ValueError as exc:
        return _back("/punches", error=str(exc))

    audit.record(
        db,
        "create",
        "punch",
        punch.id,
        after=audit.punch_snapshot(punch),
        actor=actor,
    )
    db.commit()
    return _back("/punches", message=f"Bélyegzés felvéve: {fmt_dt(ts_utc)}")


@router.post("/punches/{punch_id}/direction")
def punch_set_direction(
    punch_id: int,
    direction: str = Form(default=""),
    db: Session = Depends(get_db),
    actor: str = Depends(require_admin),
) -> RedirectResponse:
    punch = db.get(Punch, punch_id)
    if punch is None:
        return _back("/punches", error="Nincs ilyen bélyegzés")

    before = audit.punch_snapshot(punch)
    punch_service.override_punch_direction(
        db, punch, direction if direction in ("IN", "OUT") else None
    )
    audit.record(
        db,
        "override_direction",
        "punch",
        punch.id,
        before=before,
        after=audit.punch_snapshot(punch),
        actor=actor,
    )
    db.commit()
    return _back("/punches", message="Irány módosítva, az újraszámolás lefutott")


@router.post("/punches/{punch_id}/delete")
def punch_delete(
    punch_id: int,
    db: Session = Depends(get_db),
    actor: str = Depends(require_admin),
) -> RedirectResponse:
    punch = db.get(Punch, punch_id)
    if punch is None:
        return _back("/punches", error="Nincs ilyen bélyegzés")

    before = audit.punch_snapshot(punch)
    punch_service.delete_punch(db, punch)
    audit.record(db, "delete", "punch", before["id"], before=before, actor=actor)
    db.commit()
    return _back("/punches", message="Bélyegzés törölve, az újraszámolás lefutott")


# A bér-nézetek ezerelválasztós formázása. A definíció után regisztráljuk,
# hogy a templatek is elérjék {{ osszeg|ft }} alakban.
templates.env.filters["ft"] = _ft
