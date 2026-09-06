"""Háttérfeladat: a nyitva felejtett munkamenetek automatikus zárása.

Az `AUTO_CLOSE_HOUR` időpontban (default 23:59, lokális idő szerint) fut.
A tényleges zárási logika a `punches.close_stale_sessions`-ben van, hogy
ugyanazt a szabályt használja az újraszámolás is – így egy késve érkező
esemény feldolgozása után sem támad fel egy már lezárt nap.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from ..config import settings
from ..db import SessionLocal
from ..timeutil import local_clock_utc, to_local, utcnow
from .punches import close_stale_sessions

log = logging.getLogger("iroda.autoclose")


def next_run_utc(now: datetime | None = None) -> datetime:
    """A következő napzárási időpont UTC-ben."""
    now = now or utcnow()
    today_local = to_local(now).date()
    candidate = local_clock_utc(today_local, settings.auto_close_hour, settings.auto_close_minute)
    if candidate <= now:
        candidate = local_clock_utc(
            today_local + timedelta(days=1), settings.auto_close_hour, settings.auto_close_minute
        )
    return candidate


async def run_forever() -> None:
    """A lifespan által indított, végtelen ciklusú háttértask."""
    log.info("Automatikus napzárás ütemezve %s-kor (%s)", settings.auto_close_label, settings.tz_name)
    while True:
        target = next_run_utc()
        delay = max((target - utcnow()).total_seconds(), 1.0)
        try:
            # Legfeljebb egy órát alszunk egyszerre, hogy egy óraátállítás vagy
            # egy hosszú alvásból ébredő konténer ne csússzon el a céltól.
            await asyncio.sleep(min(delay, 3600.0))
        except asyncio.CancelledError:
            log.info("Az automatikus napzárás leállt.")
            raise

        if utcnow() < target:
            continue

        try:
            with SessionLocal() as db:
                close_stale_sessions(db)
        except Exception:  # pragma: no cover - a háttértask soha ne haljon meg
            log.exception("Az automatikus napzárás hibára futott")

        # Biztos, ami biztos: ne fussunk kétszer ugyanabban a percben.
        await asyncio.sleep(61)
