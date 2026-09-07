"""Bérszámítás a ledolgozott órákból.

Tájékoztató bruttó összeg: óra × órabér. Nincs benne adó, járulék, pótlék,
szabadság vagy táppénz — ez nem bérszámfejtés.

Két szabály tartja együtt az egészet:

1. **Egyszer kerekítünk**, a napi összeg végén. A másodperceket összegezzük,
   és csak utána váltjuk forintra. Óránkénti vagy munkamenetenkénti kerekítés
   percek tucatjait tüntetné el egy hónap alatt.
2. **A havi összeg a napi összegek szummája**, nem a hónap nyers
   másodperceiből újraszámolt érték. Így a felületen megjelenő napi sorok
   pontosan kiadják a havi végösszeget — az irodavezetőnek nem kell azon
   gondolkodnia, miért nem stimmel.
"""

from __future__ import annotations

from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Employee, EmployeeRate
from ..timeutil import today_local, utcnow
from .reports import SessionRow, collect_sessions

SECONDS_PER_HOUR = Decimal(3600)


def forint(seconds: int, hourly_rate: int) -> int:
    """Másodperc és órabér -> egész forint.

    `Decimal`-lal számol, nem float-tal, és félnél felfelé kerekít. A Python
    beépített `round()`-ja bankári kerekítést használ (`round(2.5) == 2`),
    ami pénznél meglepő és nehezen magyarázható.
    """
    if seconds <= 0 or hourly_rate == 0:
        return 0
    amount = (Decimal(int(seconds)) / SECONDS_PER_HOUR) * Decimal(int(hourly_rate))
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def hours_of(seconds: int) -> float:
    return round(seconds / 3600, 2) if seconds > 0 else 0.0


# --------------------------------------------------------------------------
# Órabér feloldása az előzményből
# --------------------------------------------------------------------------
class RateResolver:
    """Egy adott naphoz tartozó órabért old fel, memóriában.

    Az érintett dolgozók összes bérsorát egyszer tölti be, utána naponként
    már nem indít lekérdezést.
    """

    def __init__(self, db: Session, employee_ids: list[int] | None = None) -> None:
        query = select(EmployeeRate).order_by(
            EmployeeRate.employee_id, EmployeeRate.valid_from, EmployeeRate.id
        )
        if employee_ids is not None:
            if not employee_ids:
                self._by_employee: dict[int, list[EmployeeRate]] = {}
                return
            query = query.where(EmployeeRate.employee_id.in_(employee_ids))

        by_employee: dict[int, list[EmployeeRate]] = defaultdict(list)
        for row in db.scalars(query):
            by_employee[row.employee_id].append(row)
        self._by_employee = dict(by_employee)

    def resolve(self, employee_id: int, day: date) -> tuple[int, bool]:
        """(órabér, alapértelmezett-e) az adott dolgozóra és napra.

        Az a sor érvényes, aminek a `valid_from` értéke a legnagyobb az adott
        dátumnál nem későbbiek közül. Ha nincs ilyen, a HOURLY_RATE env érték.
        """
        chosen: EmployeeRate | None = None
        for row in self._by_employee.get(employee_id, ()):
            if row.valid_from <= day:
                chosen = row
            else:
                break  # valid_from szerint rendezve érkeztek
        if chosen is None:
            return settings.hourly_rate, True
        return chosen.hourly_rate, False


def rate_history(db: Session, employee_id: int) -> list[EmployeeRate]:
    """A dolgozó órabér-előzménye, a legfrissebb elöl."""
    return list(
        db.scalars(
            select(EmployeeRate)
            .where(EmployeeRate.employee_id == employee_id)
            .order_by(EmployeeRate.valid_from.desc(), EmployeeRate.id.desc())
        )
    )


def current_rate(db: Session, employee_id: int, day: date | None = None) -> tuple[int, bool]:
    return RateResolver(db, [employee_id]).resolve(employee_id, day or today_local())


def add_rate(
    db: Session,
    employee_id: int,
    hourly_rate: int,
    valid_from: date,
    created_by: str = "admin",
    note: str | None = None,
) -> EmployeeRate:
    """Új órabér-sor. Meglévőt soha nem írunk felül."""
    row = EmployeeRate(
        employee_id=employee_id,
        hourly_rate=hourly_rate,
        valid_from=valid_from,
        created_at=utcnow(),
        created_by=created_by,
        note=note,
    )
    db.add(row)
    db.flush()
    return row


# --------------------------------------------------------------------------
# Napi és havi számítás
# --------------------------------------------------------------------------
@dataclass
class SalaryDay:
    """Egy dolgozó egy napja. Az `amount` itt kerekül forintra, egyszer."""

    employee_id: int
    employee_name: str
    employee_code: str
    local_date: date
    hourly_rate: int
    rate_is_default: bool
    sessions: list[SessionRow] = field(default_factory=list)
    seconds: int = 0
    auto_closed_seconds: int = 0
    auto_closed_count: int = 0

    @property
    def hours(self) -> float:
        return hours_of(self.seconds)

    @property
    def amount(self) -> int:
        return forint(self.seconds, self.hourly_rate)

    @property
    def auto_closed_hours(self) -> float:
        return hours_of(self.auto_closed_seconds)

    @property
    def auto_closed_amount(self) -> int:
        """Ebből ellenőrzést igénylő rész.

        Külön kerekül, mert "ebből" adat, nem összeadandó tétel — így a napi
        összeghez képest legfeljebb egy forint eltérés lehet benne.
        """
        return forint(self.auto_closed_seconds, self.hourly_rate)

    @property
    def needs_check(self) -> bool:
        return self.auto_closed_count > 0

    @property
    def first_in(self):
        return self.sessions[0].started_at if self.sessions else None

    @property
    def last_out(self):
        return self.sessions[-1].ended_at if self.sessions else None


@dataclass
class SalaryEmployeeMonth:
    """Egy dolgozó egy hónapja. Minden összeg a napi sorokból adódik össze."""

    employee_id: int
    employee_name: str
    employee_code: str
    month: str
    days: list[SalaryDay] = field(default_factory=list)

    @property
    def seconds(self) -> int:
        return sum(day.seconds for day in self.days)

    @property
    def hours(self) -> float:
        return hours_of(self.seconds)

    @property
    def amount(self) -> int:
        # A napi összegek szummája -- NEM a hónap másodperceiből újraszámolva.
        return sum(day.amount for day in self.days)

    @property
    def auto_closed_days(self) -> int:
        return sum(1 for day in self.days if day.needs_check)

    @property
    def auto_closed_seconds(self) -> int:
        return sum(day.auto_closed_seconds for day in self.days)

    @property
    def auto_closed_hours(self) -> float:
        return hours_of(self.auto_closed_seconds)

    @property
    def auto_closed_amount(self) -> int:
        return sum(day.auto_closed_amount for day in self.days)

    @property
    def uses_default_rate(self) -> bool:
        return any(day.rate_is_default for day in self.days)

    @property
    def average_rate(self) -> int:
        """Súlyozott átlagos órabér: a kifizetendő összeg és az órák hányadosa."""
        if self.seconds <= 0:
            return 0
        amount = Decimal(self.amount) * SECONDS_PER_HOUR / Decimal(self.seconds)
        return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    @property
    def distinct_rates(self) -> list[int]:
        return sorted({day.hourly_rate for day in self.days})


def month_bounds(month: str) -> tuple[date, date]:
    """"2026-09" -> (2026-09-01, 2026-09-30)."""
    year, mon = (int(part) for part in month.split("-", 1))
    first = date(year, mon, 1)
    last = date(year + (mon == 12), (mon % 12) + 1, 1) - timedelta(days=1)
    return first, last


def parse_month(raw: str | None, fallback: date) -> str:
    """Elfogad "2026-09" alakot; hibás bemenetnél a tartalék hónapot adja."""
    text = (raw or "").strip()
    if len(text) == 7 and text[4] == "-":
        try:
            month_bounds(text)
            return text
        except (ValueError, IndexError):
            pass
    return fallback.strftime("%Y-%m")


def salary_days(
    db: Session, first: date, last: date, employee_id: int | None = None
) -> list[SalaryDay]:
    """Napi bér-sorok a megadott lokális dátumtartományra.

    A nyitott (le nem zárt) munkamenetek kimaradnak: amíg valaki bent van,
    nincs mit kifizetni. Az `auto_closed` munkamenetek viszont beleszámítanak
    — csak külön is összesítjük őket, mert kézi jóváhagyást igényelnek.
    """
    rows = [row for row in collect_sessions(db, first, last, employee_id) if not row.is_open]
    resolver = RateResolver(db, sorted({row.employee_id for row in rows}))

    groups: OrderedDict[tuple[int, date], SalaryDay] = OrderedDict()
    for row in sorted(rows, key=lambda r: (r.employee_name, r.local_date, r.started_at)):
        key = (row.employee_id, row.local_date)
        day = groups.get(key)
        if day is None:
            hourly_rate, is_default = resolver.resolve(row.employee_id, row.local_date)
            day = SalaryDay(
                employee_id=row.employee_id,
                employee_name=row.employee_name,
                employee_code=row.employee_code,
                local_date=row.local_date,
                hourly_rate=hourly_rate,
                rate_is_default=is_default,
            )
            groups[key] = day
        day.sessions.append(row)
        day.seconds += row.seconds
        if row.auto_closed:
            day.auto_closed_seconds += row.seconds
            day.auto_closed_count += 1
    return list(groups.values())


def group_by_employee_month(days: list[SalaryDay], month: str) -> list[SalaryEmployeeMonth]:
    groups: OrderedDict[int, SalaryEmployeeMonth] = OrderedDict()
    for day in sorted(days, key=lambda d: (d.employee_name, d.local_date)):
        group = groups.get(day.employee_id)
        if group is None:
            group = SalaryEmployeeMonth(
                employee_id=day.employee_id,
                employee_name=day.employee_name,
                employee_code=day.employee_code,
                month=month,
            )
            groups[day.employee_id] = group
        group.days.append(day)
    return list(groups.values())


def employee_month(db: Session, employee_id: int, month: str) -> SalaryEmployeeMonth:
    """Egy dolgozó egy hónapja, napi bontásban."""
    first, last = month_bounds(month)
    days = salary_days(db, first, last, employee_id)
    groups = group_by_employee_month(days, month)
    if groups:
        return groups[0]

    # Nincs adat: üres, de névvel kitöltött csoportot adunk vissza, hogy a
    # felület akkor is tudja, kiről van szó.
    employee = db.get(Employee, employee_id)
    return SalaryEmployeeMonth(
        employee_id=employee_id,
        employee_name=employee.name if employee else "?",
        employee_code=employee.employee_code if employee else "",
        month=month,
    )


def month_summary(db: Session, month: str) -> list[SalaryEmployeeMonth]:
    """Egy hónap minden dolgozóval — ez a nézet megy a könyvelőnek."""
    first, last = month_bounds(month)
    return group_by_employee_month(salary_days(db, first, last), month)


@dataclass
class SalaryTotals:
    """Több dolgozó összesítése egy nézet aljára."""

    seconds: int = 0
    amount: int = 0
    auto_closed_days: int = 0
    auto_closed_seconds: int = 0
    auto_closed_amount: int = 0

    @property
    def hours(self) -> float:
        return hours_of(self.seconds)

    @property
    def auto_closed_hours(self) -> float:
        return hours_of(self.auto_closed_seconds)


def totals_of(groups: list[SalaryEmployeeMonth]) -> SalaryTotals:
    return SalaryTotals(
        seconds=sum(group.seconds for group in groups),
        amount=sum(group.amount for group in groups),
        auto_closed_days=sum(group.auto_closed_days for group in groups),
        auto_closed_seconds=sum(group.auto_closed_seconds for group in groups),
        auto_closed_amount=sum(group.auto_closed_amount for group in groups),
    )
