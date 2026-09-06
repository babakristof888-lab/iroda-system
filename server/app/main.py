"""Iroda RFID munkaidő-nyilvántartó – FastAPI alkalmazás.

Indítás Railway-en:
    uvicorn app.main:app --host 0.0.0.0 --port $PORT
a service Root Directory-ja a repóban `server`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .api import v1 as api_v1
from .config import settings
from .db import StorageError, init_db, set_startup_error
from .security import AuthRedirect
from .services import autoclose
from .web import routes as web_routes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("iroda")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")


def _log_configuration() -> None:
    log.info("Iroda szerver %s indul", settings.version)
    log.info("  DB_PATH=%s  TZ=%s", settings.db_path, settings.tz_name)
    log.info(
        "  DEBOUNCE_SECONDS=%s  AUTO_CLOSE_HOUR=%s",
        settings.debounce_seconds,
        settings.auto_close_label,
    )
    log.info(
        "  Környezeti riasztás: %s (hőm. %.0f–%.0f °C, pára %.0f–%.0f %%)",
        "be" if settings.env_alerts_enabled else "ki",
        settings.temp_min_alert,
        settings.temp_max_alert,
        settings.hum_min_alert,
        settings.hum_max_alert,
    )
    if not settings.gateway_api_key:
        log.error("A GATEWAY_API_KEY nincs beállítva – a gateway minden hívása 401-et kap.")
    if not settings.admin_password_hash:
        log.error(
            "Az ADMIN_PASSWORD_HASH nincs beállítva – nem lehet belépni az admin felületre. "
            "Hash generálása: python scripts/hash_password.py"
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _log_configuration()
    background: asyncio.Task | None = None
    try:
        init_db()
        background = asyncio.create_task(autoclose.run_forever())
    except StorageError as exc:
        # Nem crash-loopolunk: a hibát egyszer, érthetően kiírjuk, és a
        # healthcheck 503-mal jelzi, hogy a szolgáltatás nem üzemképes.
        set_startup_error(str(exc))
        log.critical("INDÍTÁSI HIBA – az adatbázis nem használható")
        log.critical("%s", exc)

    try:
        yield
    finally:
        if background is not None:
            background.cancel()
            try:
                await background
            except asyncio.CancelledError:
                pass


app = FastAPI(
    title="Iroda RFID munkaidő-nyilvántartó",
    version=settings.version,
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)


@app.exception_handler(AuthRedirect)
async def _auth_redirect(request: Request, exc: AuthRedirect) -> RedirectResponse:
    target = "/login"
    if exc.next_url and exc.next_url not in ("/", "/login"):
        target = f"/login?next={exc.next_url}"
    return RedirectResponse(target, status_code=303)


@app.exception_handler(StorageError)
async def _storage_error(request: Request, exc: StorageError) -> JSONResponse:
    log.critical("Tárolási hiba kérés közben: %s", exc)
    return JSONResponse({"status": "error", "detail": str(exc)}, status_code=503)


if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

app.include_router(api_v1.router)
app.include_router(web_routes.router)
