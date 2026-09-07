"""Közös teszt-környezet.

Se soros port, se hálózat, se `C:\\ProgramData` – minden teszt ideiglenes
könyvtárban dolgozik, és a hardvert mockoljuk.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

GATEWAY_DIR = Path(__file__).resolve().parent.parent
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import db  # noqa: E402
from config import Config  # noqa: E402


@pytest.fixture()
def cfg(tmp_path: Path) -> Config:
    return Config(
        server_url="https://teszt.example",
        api_key="teszt-kulcs",
        gateway_id="iroda-teszt",
        serial_port=None,
        baud_rate=115200,
        tz_name="Europe/Budapest",
        data_dir=tmp_path,
        log_level="DEBUG",
        env_enabled=True,
    )


@pytest.fixture()
def conn(cfg: Config):
    connection = db.connect(cfg.db_path)
    db.init_schema(connection)
    yield connection
    connection.close()


class FakeResponse:
    """A `requests.Response` minimuma, amit az uploader használ."""

    def __init__(self, status_code: int = 200, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or ("" if payload is None else str(payload))

    def json(self):
        if self._payload is None:
            raise ValueError("nem JSON")
        return self._payload


class FakeSession:
    """`requests.Session` helyettesítő: rögzíti a hívásokat, előre megadott
    válaszokat ad vissza. Egy `Exception` a listában hálózati hibát szimulál."""

    def __init__(self, responses=None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):  # noqa: A002
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        if not self.responses:
            return FakeResponse(200, {"accepted": [], "duplicates": [], "errors": []})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture()
def fake_session():
    return FakeSession
