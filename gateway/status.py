"""A szálak közös állapota: ez megy fel a heartbeatben.

Szándékosan pici: két logikai érték és a nyitott port neve. Semmi üzleti
adat nem lakik itt.
"""

from __future__ import annotations

import threading


class Status:
    def __init__(self, serial_ok: bool = False, bme_ok: bool | None = None) -> None:
        self._lock = threading.Lock()
        self._serial_ok = serial_ok
        self._bme_ok = bme_ok
        self._port: str | None = None

    # -- soros port / RFID olvasó -----------------------------------------
    def set_serial_ok(self, value: bool) -> None:
        with self._lock:
            self._serial_ok = bool(value)

    @property
    def serial_ok(self) -> bool:
        with self._lock:
            return self._serial_ok

    # -- BME280 ------------------------------------------------------------
    def set_bme_ok(self, value: bool | None) -> None:
        with self._lock:
            self._bme_ok = None if value is None else bool(value)

    @property
    def bme_ok(self) -> bool | None:
        with self._lock:
            return self._bme_ok

    # -- melyik porton vagyunk (a diagnosztikához) -------------------------
    def set_port(self, value: str | None) -> None:
        with self._lock:
            self._port = value

    @property
    def port(self) -> str | None:
        with self._lock:
            return self._port
