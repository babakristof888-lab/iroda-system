"""Idő- és időzónakezelés.

Egyetlen szabály, amit az egész alkalmazás betart:

* az adatbázisban **minden TIMESTAMP naiv UTC**,
* a megjelenítés és a naptári logika (nap, hónap, munkanap határa)
  `Europe/Budapest` szerint történik.

Így a nyári/téli időszámítás váltása nem tud elrontani egyetlen
időtartam-számítást sem: két UTC pillanat különbsége mindig a valóban eltelt
idő, a naptári határokat pedig mindig a lokális zónában képezzük, majd
konvertáljuk UTC-re.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .config import settings

UTC = timezone.utc


def tz():
    """Az aktuálisan konfigurált megjelenítési időzóna."""
    return settings.tz


def utcnow() -> datetime:
    """Mostani idő naiv UTC-ként (ahogy az adatbázisban tároljuk)."""
    return datetime.now(UTC).replace(tzinfo=None)


def as_utc_naive(dt: datetime) -> datetime:
    """Tetszőleges datetime -> naiv UTC.

    Ha a bemenet naiv, lokális (Budapest) időnek tekintjük – ez a
    legkevésbé meglepő értelmezés egy magyar telephelyen álló gateway-től.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz())
    return dt.astimezone(UTC).replace(tzinfo=None)


def parse_iso_to_utc(raw: str) -> datetime:
    """A gateway által küldött ISO-8601 stringből naiv UTC.

    `ValueError`-t dob, ha nem értelmezhető – a hívó ebből csinál
    `invalid_timestamp` hibát.
    """
    if not isinstance(raw, str):
        raise ValueError("a timestamp nem szöveg")
    text = raw.strip()
    if not text:
        raise ValueError("üres timestamp")
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    # A " " elválasztó is gyakori ISO-variáns.
    if len(text) > 10 and text[10] == " ":
        text = text[:10] + "T" + text[11:]
    return as_utc_naive(datetime.fromisoformat(text))


def to_local(dt: datetime | None):
    """Naiv UTC -> zónahelyes lokális datetime (aware)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(tz())


def local_date_of(dt: datetime) -> date:
    """Melyik lokális naptári napra esik ez az UTC pillanat."""
    return to_local(dt).date()


def local_month_of(dt: datetime) -> str:
    return to_local(dt).strftime("%Y-%m")


def local_naive_to_utc(local_dt: datetime) -> datetime:
    """Lokális falióra-idő (naiv) -> naiv UTC."""
    return local_dt.replace(tzinfo=tz()).astimezone(UTC).replace(tzinfo=None)


def local_day_start_utc(day: date) -> datetime:
    """A lokális nap 00:00-ja UTC-ben.

    Budapesten az óraátállítás 02:00/03:00-kor történik, így az éjfél mindig
    létező, egyértelmű időpont – nincs szükség fold-kezelésre.
    """
    return local_naive_to_utc(datetime(day.year, day.month, day.day, 0, 0, 0))


def local_day_bounds_utc(day: date) -> tuple[datetime, datetime]:
    """[nap 00:00, következő nap 00:00) UTC-ben. DST-napokon 23 vagy 25 óra."""
    return local_day_start_utc(day), local_day_start_utc(day + timedelta(days=1))


def local_range_bounds_utc(first: date, last: date) -> tuple[datetime, datetime]:
    """Zárt lokális dátumtartomány -> félig nyílt UTC intervallum."""
    return local_day_start_utc(first), local_day_start_utc(last + timedelta(days=1))


def local_clock_utc(day: date, hour: int, minute: int) -> datetime:
    """Egy adott lokális napon adott falióra-időpont UTC-ben."""
    return local_naive_to_utc(datetime(day.year, day.month, day.day, hour, minute))


def fmt_dt(dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    local = to_local(dt)
    return local.strftime(fmt) if local else ""


def fmt_time(dt: datetime | None) -> str:
    return fmt_dt(dt, "%H:%M:%S")


def fmt_date(dt: datetime | None) -> str:
    return fmt_dt(dt, "%Y-%m-%d")


def fmt_hours(seconds: float | int | None) -> str:
    """Másodperc -> "7ó 32p" alakú, ránézésre olvasható időtartam."""
    if not seconds or seconds <= 0:
        return "0ó 00p"
    total_minutes = int(round(seconds / 60))
    return f"{total_minutes // 60}ó {total_minutes % 60:02d}p"


def hours_of(seconds: float | int | None) -> float:
    if not seconds or seconds <= 0:
        return 0.0
    return round(seconds / 3600, 2)


def today_local() -> date:
    return to_local(utcnow()).date()
