"""Naplózás.

Két helyre írunk egyszerre:

* `C:\\ProgramData\\IrodaGateway\\logs\\gateway.log` – forgatott fájl,
  5 MB × 3 példány,
* stdout – ezt az NSSM külön fájlba (`out.log`) irányítja, így a szolgáltatás
  kimenete akkor is megvan, ha a fájl-handler valamiért nem tud írni.

A formátum tartalmazza az időbélyeget és a szál nevét: egy hibakeresésnél az a
legfontosabb kérdés, hogy melyik szál csinálta.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3


def setup_logging(log_file: Path | None, level: str = "INFO") -> logging.Logger:
    """Beállítja a gyökér loggert. Többször is meghívható."""
    resolved = getattr(logging, str(level).upper(), logging.INFO)
    if not isinstance(resolved, int):
        resolved = logging.INFO

    root = logging.getLogger()
    root.setLevel(resolved)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    if log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                str(log_file),
                maxBytes=MAX_BYTES,
                backupCount=BACKUP_COUNT,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except OSError as exc:
            # A stdout marad; a szolgáltatás ettől még fut.
            root.error("A naplófájl nem nyitható meg (%s): %s", log_file, exc)

    # A requests/urllib3 alap szinten túl bőbeszédű.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return root
