"""Adatbázis-kapcsolat (SQLite, WAL módban).

Nincs Postgres és nincs `DATABASE_URL`: a Railway service-en egy perzisztens
volume van a `/data` mountponton, az adatbázis egyetlen fájl azon belül.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import settings
from .models import Base

log = logging.getLogger("iroda.db")


class StorageError(RuntimeError):
    """Az adatbázis helye nem használható. Érthető üzenettel, nem nyers sqlite3 hibával."""


def _sqlite_url(db_path: str) -> str:
    if db_path == ":memory:":
        return "sqlite://"
    return f"sqlite:///{os.path.abspath(db_path)}"


def ensure_storage(db_path: str | None = None) -> None:
    """Ellenőrzi, hogy az adatbázis mappája létezik-e és írható-e.

    Ez fut le legelőször induláskor, hogy a logban ne egy
    `sqlite3.OperationalError: unable to open database file` álljon, hanem az,
    hogy pontosan mi a teendő.
    """
    db_path = db_path or settings.db_path
    if db_path == ":memory:":
        return

    directory = os.path.dirname(os.path.abspath(db_path)) or "."
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        raise StorageError(
            f"Az adatbázis mappája nem hozható létre: {directory} ({exc.strerror}). "
            f"A DB_PATH értéke jelenleg '{db_path}'. Railway-en a service-hez "
            f"csatolt persistent volume mountpointja legyen /data, a DB_PATH pedig "
            f"/data/iroda.db. Lokális fejlesztéshez állítsd DB_PATH=./dev.db értékre."
        ) from exc

    if not os.access(directory, os.W_OK):
        raise StorageError(
            f"Az adatbázis mappája létezik, de nem írható: {directory}. "
            f"Railway-en ez általában azt jelenti, hogy a volume nincs a service-hez "
            f"csatolva, vagy nem a /data mountpointra van kötve. Ellenőrizd a "
            f"service Volumes fülét, majd indítsd újra a deploymentet."
        )

    if os.path.exists(db_path) and not os.access(db_path, os.W_OK):
        raise StorageError(
            f"Az adatbázis fájl létezik, de nem írható: {db_path}. "
            f"Ellenőrizd a volume jogosultságait."
        )


def _create_engine(db_path: str) -> Engine:
    engine = create_engine(
        _sqlite_url(db_path),
        connect_args={"check_same_thread": False, "timeout": 30},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        try:
            # WAL: az olvasók (admin felület) nem blokkolják az írókat (gateway).
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    return engine


engine: Engine = _create_engine(settings.db_path)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


# Ha az induláskori előkészítés elbukik, itt marad az emberi nyelvű ok, és a
# healthcheck ezt jelenti vissza – nem egy nyers sqlite3 hibát.
startup_error: str | None = None


def init_db() -> None:
    """Induláskori "migráció": létrehozza a hiányzó táblákat és indexeket."""
    global startup_error
    ensure_storage()
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        mode = conn.execute(text("PRAGMA journal_mode")).scalar()
    startup_error = None
    log.info("Adatbázis kész: %s (journal_mode=%s)", settings.db_path, mode)


def set_startup_error(message: str) -> None:
    global startup_error
    startup_error = message


def check_db() -> bool:
    """Healthcheckhez: tényleg elérhető-e az adatbázis."""
    if startup_error:
        return False
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # pragma: no cover - csak hibás környezetben fut
        log.exception("Az adatbázis nem elérhető")
        return False


def get_db() -> Iterator[Session]:
    """FastAPI dependency: kérésenként egy session, végén garantált lezárás."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
