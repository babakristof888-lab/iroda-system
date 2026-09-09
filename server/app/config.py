"""Központi konfiguráció.

Minden beállítás környezeti változóból jön, értelmes defaultokkal. Az itt
felolvasott értékek a folyamat indulásakor fagynak be (`settings` singleton),
így nem fordulhat elő, hogy két kérés más küszöbbel dolgozik.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Europe/Budapest"
VERSION = "1.0.0"

# A gateway batch-méret korlátai. A szerződés része, ezért konstans.
MAX_EVENTS_PER_BATCH = 500
MAX_READINGS_PER_BATCH = 200

# BME280 fizikai mérési tartományai – ezen kívül eső érték hibás mérés.
TEMP_RANGE = (-40.0, 85.0)
HUM_RANGE = (0.0, 100.0)
PRESS_RANGE = (300.0, 1100.0)


def _env_str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_float(name: str, default: float) -> float:
    raw = _env_str(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = _env_str(name)
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_str(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on", "igen")


def _parse_clock(raw: str, default: tuple[int, int]) -> tuple[int, int]:
    """`AUTO_CLOSE_HOUR` elfogad "23:59" és "23" alakot is."""
    raw = raw.strip()
    if not raw:
        return default
    try:
        if ":" in raw:
            h, m = raw.split(":", 1)
            hour, minute = int(h), int(m)
        else:
            hour, minute = int(float(raw)), 0
    except ValueError:
        return default
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return default
    return hour, minute


@dataclass(frozen=True)
class Settings:
    db_path: str
    gateway_api_key: str
    admin_password_hash: str
    session_secret: str
    cookie_secure: bool
    session_max_age_seconds: int
    tz_name: str
    debounce_seconds: int
    auto_close_hour: int
    auto_close_minute: int
    temp_min_alert: float
    temp_max_alert: float
    hum_min_alert: float
    hum_max_alert: float
    env_alerts_enabled: bool
    salary_enabled: bool
    hourly_rate: int
    gateway_offline_minutes: int
    version: str = VERSION
    tz: ZoneInfo = field(init=False)

    def __post_init__(self) -> None:
        try:
            tz = ZoneInfo(self.tz_name)
        except Exception:
            tz = ZoneInfo(DEFAULT_TZ)
        object.__setattr__(self, "tz", tz)

    @property
    def auto_close_label(self) -> str:
        return f"{self.auto_close_hour:02d}:{self.auto_close_minute:02d}"


def load_settings() -> Settings:
    admin_hash = _env_str("ADMIN_PASSWORD_HASH")

    # A session-titok sorrendje szándékos: ha nincs külön megadva, az admin
    # jelszó hash-éből származtatjuk, hogy egy újraindítás (Railway deploy) ne
    # léptessen ki mindenkit. Ha még az sincs, marad az esetleges kulcs.
    session_secret = _env_str("SESSION_SECRET")
    if not session_secret:
        if admin_hash:
            session_secret = hashlib.sha256(
                b"iroda-session|" + admin_hash.encode("utf-8")
            ).hexdigest()
        else:
            session_secret = secrets.token_urlsafe(32)

    auto_hour, auto_minute = _parse_clock(_env_str("AUTO_CLOSE_HOUR"), (23, 59))

    return Settings(
        db_path=_env_str("DB_PATH", "/data/iroda.db"),
        gateway_api_key=_env_str("GATEWAY_API_KEY"),
        admin_password_hash=admin_hash,
        session_secret=session_secret,
        cookie_secure=_env_bool("COOKIE_SECURE", True),
        session_max_age_seconds=_env_int("SESSION_MAX_AGE_SECONDS", 14 * 24 * 3600),
        tz_name=_env_str("TZ", DEFAULT_TZ),
        debounce_seconds=_env_int("DEBOUNCE_SECONDS", 60),
        auto_close_hour=auto_hour,
        auto_close_minute=auto_minute,
        temp_min_alert=_env_float("TEMP_MIN_ALERT", 16.0),
        temp_max_alert=_env_float("TEMP_MAX_ALERT", 28.0),
        hum_min_alert=_env_float("HUM_MIN_ALERT", 25.0),
        hum_max_alert=_env_float("HUM_MAX_ALERT", 65.0),
        env_alerts_enabled=_env_bool("ENV_ALERTS_ENABLED", True),
        salary_enabled=_env_bool("SALARY_ENABLED", True),
        # Csak alapértelmezés: az a dolgozó számol vele, akinek nincs saját
        # órabér-sora az employee_rates táblában.
        hourly_rate=_env_int("HOURLY_RATE", 2000),
        gateway_offline_minutes=_env_int("GATEWAY_OFFLINE_MINUTES", 5),
    )


settings = load_settings()


def reload_settings() -> Settings:
    """Csak tesztekhez: újraolvassa a környezetet."""
    global settings
    settings = load_settings()
    return settings
