"""Riportok: napi bontás és havi összesítés a munkamenetekből.

A csoportosítás mindig a munkamenet kezdetének **lokális** naptári napja
szerint történik, az időtartam viszont két UTC pillanat különbsége. Így az
óraátállítás napján is a ténylegesen eltelt idő jön ki.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Employee, Punch, WorkSession
from ..timeutil import (
    local_date_of,
    local_range_bounds_utc,
    to_local,
    utcnow,
)


@dataclass
class SessionRow:
    """Egy munkamenet a riportban megjeleníthető formában."""

    session_id: int
    employee_id: int
    employee_name: str
    employee_code: str
    local_date: date
    started_at: datetime
    ended_at: datetime | None
    seconds: int
    is_open: bool
    auto_closed: bool

    @property
    def hours(self) -> float:
        return round(self.seconds / 3600, 2)


@dataclass
class DayGroup:
    employee_id: int
    employee_name: str
    local_date: date
    rows: list[SessionRow] = field(default_factory=list)

    @property
    def seconds(self) -> int:
        return sum(r.seconds for r in self.rows)

    @property
    def hours(self) -> float:
        return round(self.seconds / 3600, 2)

    @property
    def has_auto_closed(self) -> bool:
        return any(r.auto_closed for r in self.rows)

    @property
    def has_open(self) -> bool:
        return any(r.is_open for r in self.rows)


@dataclass
class MonthGroup:
    month: str
    employee_id: int
    employee_name: str
    seconds: int = 0
    auto_closed_count: int = 0

    @property
    def hours(self) -> float:
        return round(self.seconds / 3600, 2)

    @property
    def pay(self) -> int:
        return int(round(self.seconds / 3600 * settings.hourly_rate))


def collect_sessions(
    db: Session,
    first: date,
    last: date,
    employee_id: int | None = None,
) -> list[SessionRow]:
    start_utc, end_utc = local_range_bounds_utc(first, last)
    now = utcnow()

    query = (
        select(WorkSession, Employee)
        .join(Employee, Employee.id == WorkSession.employee_id)
        .where(WorkSession.started_at >= start_utc, WorkSession.started_at < end_utc)
        .order_by(WorkSession.started_at.asc())
    )
    if employee_id is not None:
        query = query.where(WorkSession.employee_id == employee_id)

    rows: list[SessionRow] = []
    for session, employee in db.execute(query).all():
        is_open = session.ended_at is None
        if is_open:
            # A még nyitott munkamenetnél az eddig eltelt időt mutatjuk.
            seconds = max(int((now - session.started_at).total_seconds()), 0)
        else:
            seconds = int(session.duration_seconds or 0)
        rows.append(
            SessionRow(
                session_id=session.id,
                employee_id=employee.id,
                employee_name=employee.name,
                employee_code=employee.employee_code,
                local_date=local_date_of(session.started_at),
                started_at=session.started_at,
                ended_at=session.ended_at,
                seconds=seconds,
                is_open=is_open,
                auto_closed=bool(session.auto_closed),
            )
        )
    return rows


def group_daily(rows: list[SessionRow]) -> list[DayGroup]:
    groups: OrderedDict[tuple[date, int], DayGroup] = OrderedDict()
    for row in sorted(rows, key=lambda r: (r.local_date, r.employee_name, r.started_at)):
        key = (row.local_date, row.employee_id)
        group = groups.get(key)
        if group is None:
            group = DayGroup(
                employee_id=row.employee_id,
                employee_name=row.employee_name,
                local_date=row.local_date,
            )
            groups[key] = group
        group.rows.append(row)
    return list(groups.values())


def group_monthly(rows: list[SessionRow]) -> list[MonthGroup]:
    groups: OrderedDict[tuple[str, int], MonthGroup] = OrderedDict()
    for row in rows:
        month = row.local_date.strftime("%Y-%m")
        key = (month, row.employee_id)
        group = groups.get(key)
        if group is None:
            group = MonthGroup(
                month=month, employee_id=row.employee_id, employee_name=row.employee_name
            )
            groups[key] = group
        group.seconds += row.seconds
        if row.auto_closed:
            group.auto_closed_count += 1
    return sorted(groups.values(), key=lambda g: (g.month, g.employee_name))


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
def who_is_in(db: Session) -> list[dict]:
    """Nyitott munkamenetek: ki van bent MOST."""
    now = utcnow()
    query = (
        select(WorkSession, Employee)
        .join(Employee, Employee.id == WorkSession.employee_id)
        .where(WorkSession.ended_at.is_(None))
        .order_by(WorkSession.started_at.asc())
    )
    result = []
    for session, employee in db.execute(query).all():
        elapsed = max(int((now - session.started_at).total_seconds()), 0)
        result.append(
            {
                "employee_id": employee.id,
                "name": employee.name,
                "employee_code": employee.employee_code,
                "started_at": session.started_at,
                "started_local": to_local(session.started_at),
                "seconds": elapsed,
            }
        )
    return result


def punches_of_day(db: Session, day: date, limit: int = 200) -> list[tuple[Punch, Employee | None]]:
    start_utc, end_utc = local_range_bounds_utc(day, day)
    query = (
        select(Punch, Employee)
        .join(Employee, Employee.id == Punch.employee_id, isouter=True)
        .where(Punch.ts_utc >= start_utc, Punch.ts_utc < end_utc)
        .order_by(Punch.ts_utc.desc())
        .limit(limit)
    )
    return [(punch, employee) for punch, employee in db.execute(query).all()]
