"""Autentikáció.

Kétféle hívó van, kétféle módszerrel:

* a **gateway** minden `/api/v1/*` végpontot `X-API-Key` fejléccel hív
  (kivéve a `/health`-et, amit a Railway healthcheckje ver),
* az **irodavezető** a böngészőből, aláírt session cookie-val.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

import bcrypt
from fastapi import Header, HTTPException, Request, Response, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import settings

log = logging.getLogger("iroda.security")

SESSION_COOKIE = "iroda_session"
_SALT = "iroda-admin-session"


class AuthRedirect(Exception):
    """Böngészős kérés hitelesítés nélkül – a main.py átirányít a login oldalra."""

    def __init__(self, next_url: str = "/") -> None:
        self.next_url = next_url
        super().__init__(next_url)


# --------------------------------------------------------------------------
# Gateway: API kulcs
# --------------------------------------------------------------------------
def api_key_valid(candidate: str | None) -> bool:
    expected = settings.gateway_api_key
    if not expected:
        # Kulcs nélkül minden hívást elutasítunk: egy véletlenül nyitva hagyott
        # végpont rosszabb, mint egy nem működő gateway.
        log.error("A GATEWAY_API_KEY nincs beállítva – minden gateway hívást elutasítok.")
        return False
    if not candidate:
        return False
    return secrets.compare_digest(candidate.strip(), expected)


async def require_api_key(x_api_key: str | None = Header(default=None)) -> str:
    """FastAPI dependency a gateway végpontokhoz."""
    if not api_key_valid(x_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Érvénytelen vagy hiányzó X-API-Key",
        )
    return "gateway"


# --------------------------------------------------------------------------
# Admin: jelszó + session cookie
# --------------------------------------------------------------------------
def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str) -> bool:
    stored = settings.admin_password_hash
    if not stored:
        log.error("Az ADMIN_PASSWORD_HASH nincs beállítva – a belépés nem lehetséges.")
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), stored.encode("utf-8"))
    except (ValueError, TypeError):
        log.error("Az ADMIN_PASSWORD_HASH nem érvényes bcrypt hash.")
        return False


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_secret, salt=_SALT)


def issue_session(response: Response, actor: str = "admin") -> None:
    token = _serializer().dumps({"actor": actor})
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_max_age_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def read_session(request: Request) -> dict[str, Any] | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        return _serializer().loads(token, max_age=settings.session_max_age_seconds)
    except (BadSignature, SignatureExpired):
        return None


def is_logged_in(request: Request) -> bool:
    return read_session(request) is not None


async def require_admin(request: Request) -> str:
    """Dependency a HTML oldalakhoz: hitelesítés nélkül a login oldalra dob."""
    session = read_session(request)
    if session is None:
        raise AuthRedirect(next_url=request.url.path)
    return str(session.get("actor", "admin"))


async def require_admin_or_api_key(
    request: Request, x_api_key: str | None = Header(default=None)
) -> str:
    """A grafikon-adatot a böngésző kéri le session cookie-val.

    Az API kulcsot is elfogadjuk, hogy a végpont parancssorból is
    tesztelhető legyen – mindkettő hitelesített hívó.
    """
    session = read_session(request)
    if session is not None:
        return str(session.get("actor", "admin"))
    if api_key_valid(x_api_key):
        return "gateway"
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Bejelentkezés szükséges"
    )
