"""Statisztikák a grafikonokhoz.

Ez a réteg a jelenléti adatot készíti elő megjelenítésre. Ugyanabból a
`collect_sessions` forrásból dolgozik, mint a riportok, tehát a grafikonon
látható óraszám mindig egyezik a riport óraszámával.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Employee
from ..timeutil import today_local
from .reports import SessionRow, collect_sessions

WEEKDAYS = ["hétfő", "kedd", "szerda", "csütörtök", "péntek", "szombat", "vasárnap"]
WEEKDAYS_SHORT = ["H", "K", "Sze", "Cs", "P", "Szo", "V"]


# --------------------------------------------------------------------------
# Időszak: a megadott napot tartalmazó hét vagy hónap
# --------------------------------------------------------------------------
@dataclass
class Period:
    mode: str  # "week" | "month"
    first: date
    last: date

    @property
    def label(self) -> str:
        if self.mode == "week":
            return (
                f"{self.first.isocalendar().week}. hét "
                f"({self.first.isoformat()} – {self.last.isoformat()})"
            )
        return self.first.strftime("%Y. %B").replace(
            self.first.strftime("%B"), MONTHS[self.first.month - 1]
        )

    @property
    def anchor(self) -> date:
        return self.first

    def shifted(self, steps: int) -> "Period":
        """Előző/következő időszak."""
        if self.mode == "week":
            return period_of(self.first + timedelta(weeks=steps), "week")
        year, month = self.first.year, self.first.month + steps
        year += (month - 1) // 12
        month = (month - 1) % 12 + 1
        return period_of(date(year, month, 1), "month")

    @property
    def days(self) -> list[date]:
        span = (self.last - self.first).days
        return [self.first + timedelta(days=offset) for offset in range(span + 1)]


MONTHS = [
    "január", "február", "március", "április", "május", "június",
    "július", "augusztus", "szeptember", "október", "november", "december",
]


def period_of(anchor: date, mode: str = "month") -> Period:
    """A megadott napot tartalmazó hét (hétfő–vasárnap) vagy hónap."""
    if mode == "week":
        first = anchor - timedelta(days=anchor.weekday())
        return Period(mode="week", first=first, last=first + timedelta(days=6))

    first = anchor.replace(day=1)
    next_month = date(first.year + (first.month == 12), (first.month % 12) + 1, 1)
    return Period(mode="month", first=first, last=next_month - timedelta(days=1))


def parse_period(mode: str | None, anchor: str | None) -> Period:
    """A felületről érkező szűrő értelmezése, hibás bemenetnél a mai hónap."""
    mode = mode if mode in ("week", "month") else "month"
    try:
        day = date.fromisoformat((anchor or "").strip())
    except ValueError:
        day = today_local()
    return period_of(day, mode)


# --------------------------------------------------------------------------
# Napi óraszám egy dolgozóra
# --------------------------------------------------------------------------
@dataclass
class DailyBar:
    """Egy nap egy oszlopa. A nulla órás napok is szerepelnek, hogy a
    hétvégék és a hiányzások látszódjanak a grafikonon."""

    local_date: date
    seconds: int = 0
    sessions: int = 0
    auto_closed: bool = False
    open: bool = False

    @property
    def hours(self) -> float:
        return round(self.seconds / 3600, 2)

    @property
    def weekday(self) -> str:
        return WEEKDAYS[self.local_date.weekday()]

    @property
    def weekday_short(self) -> str:
        return WEEKDAYS_SHORT[self.local_date.weekday()]

    @property
    def is_weekend(self) -> bool:
        return self.local_date.weekday() >= 5


def daily_bars(db: Session, period: Period, employee_id: int) -> list[DailyBar]:
    """Napi óraszám egy dolgozóra, az időszak MINDEN napjára."""
    bars = {day: DailyBar(local_date=day) for day in period.days}

    for row in collect_sessions(db, period.first, period.last, employee_id):
        bar = bars.get(row.local_date)
        if bar is None:  # elvileg nem fordulhat elő, de ne veszítsünk adatot
            bar = bars[row.local_date] = DailyBar(local_date=row.local_date)
        bar.seconds += row.seconds
        bar.sessions += 1
        bar.auto_closed = bar.auto_closed or row.auto_closed
        bar.open = bar.open or row.is_open

    return [bars[day] for day in sorted(bars)]


# --------------------------------------------------------------------------
# Dolgozók összehasonlítása
# --------------------------------------------------------------------------
@dataclass
class EmployeeTotal:
    employee_id: int
    name: str
    employee_code: str
    seconds: int = 0
    days: set = field(default_factory=set)
    auto_closed_days: set = field(default_factory=set)
    open_count: int = 0

    @property
    def hours(self) -> float:
        return round(self.seconds / 3600, 2)

    @property
    def day_count(self) -> int:
        return len(self.days)

    @property
    def auto_closed_day_count(self) -> int:
        return len(self.auto_closed_days)

    @property
    def average_hours_per_day(self) -> float:
        return round(self.seconds / 3600 / len(self.days), 2) if self.days else 0.0


def employee_totals(db: Session, period: Period) -> list[EmployeeTotal]:
    """Ki mennyit volt bent összesen az időszakban, csökkenő sorrendben."""
    rows: list[SessionRow] = collect_sessions(db, period.first, period.last)

    totals: dict[int, EmployeeTotal] = {}
    for row in rows:
        total = totals.get(row.employee_id)
        if total is None:
            total = totals[row.employee_id] = EmployeeTotal(
                employee_id=row.employee_id,
                name=row.employee_name,
                employee_code=row.employee_code,
            )
        total.seconds += row.seconds
        total.days.add(row.local_date)
        if row.auto_closed:
            total.auto_closed_days.add(row.local_date)
        if row.is_open:
            total.open_count += 1

    return sorted(totals.values(), key=lambda t: (-t.seconds, t.name))


def active_employees(db: Session) -> list[Employee]:
    return list(db.scalars(select(Employee).order_by(Employee.active.desc(), Employee.name)))
