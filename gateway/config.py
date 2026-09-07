"""A gateway konfigurációja.

Minden beállítás a `gateway/.env` fájlból vagy a környezetből jön. Az értékeket
egyetlen `Config` objektum hordozza, amit a `main.py` állít elő és ad át a
szálaknak – szándékosan nincs globális singleton, mert így a tesztek is tudnak
saját, ideiglenes könyvtárra mutató konfigurációt gyártani.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

VERSION = "1.0.0"

GATEWAY_DIR = Path(__file__).resolve().parent
DEFAULT_TZ = "Europe/Budapest"
DEFAULT_SERVER_URL = "https://iroda-szerver.up.railway.app"
DEFAULT_GATEWAY_ID = "iroda-fszt"
DEFAULT_BAUD_RATE = 115200

# A szerver a `sensor_id` mezőt "default"-ra állítja, ha üresen hagyjuk. Egy
# gateway = egy szenzor, ezért ez konstans, nem konfigurálható.
SENSOR_ID = "default"

# --------------------------------------------------------------------------
# A feltöltés paraméterei. Ezek a szerver szerződéséből következnek
# (MAX_EVENTS_PER_BATCH=500, MAX_READINGS_PER_BATCH=200), bőven alatta maradunk.
# --------------------------------------------------------------------------
PUNCH_BATCH_SIZE = 100
ENV_BATCH_SIZE = 50
UPLOAD_INTERVAL_SECONDS = 5
BACKOFF_MAX_SECONDS = 300
HEARTBEAT_INTERVAL_SECONDS = 60
CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 15

# --------------------------------------------------------------------------
# Karbantartás
# --------------------------------------------------------------------------
MAINTENANCE_INTERVAL_SECONDS = 24 * 3600
SENT_RETENTION_DAYS = 30
SAMPLE_RETENTION_DAYS = 7

# --------------------------------------------------------------------------
# Soros port
# --------------------------------------------------------------------------
SERIAL_REOPEN_SECONDS = 2.0        # port menet közben elveszett
SERIAL_BUSY_RETRY_SECONDS = 5.0    # a portot más program tartja nyitva
SERIAL_PING_SECONDS = 60.0         # életjel-kérdés az Arduinónak
SERIAL_SILENCE_SECONDS = 300.0     # ennyi néma másodperc után újranyitjuk a portot

# Ismert USB-soros átalakítók. A klón Nanókban szinte mindig CH340 van.
USB_DEVICE_IDS: dict[tuple[int, int], str] = {
    (0x1A86, 0x7523): "CH340",
    (0x0403, 0x6001): "FTDI FT232",
    (0x10C4, 0xEA60): "CP210x",
}

# --------------------------------------------------------------------------
# A BME280 fizikai mérési tartományai. Ugyanezek a korlátok vannak a szerveren
# is (server/app/config.py); az ezen kívül eső nyers minta hibás mérés, és nem
# szabad, hogy elrontsa az órás átlagot.
# --------------------------------------------------------------------------
TEMP_RANGE = (-40.0, 85.0)
HUM_RANGE = (0.0, 100.0)
PRESS_RANGE = (300.0, 1100.0)


def _windows_program_data() -> Path:
    return Path(os.environ.get("PROGRAMDATA") or r"C:\ProgramData")


def default_data_dir() -> Path:
    """A célkörnyezet Windows: `C:\\ProgramData\\IrodaGateway`.

    Se a `Program Files` (jogosultság), se a felhasználói profil (a
    szolgáltatás nem a te fiókoddal fut) nem jó. Nem Windows alatt – ilyenkor
    fejlesztünk vagy tesztelünk – a felhasználó adatkönyvtárába kerül.
    """
    if os.name == "nt":
        return _windows_program_data() / "IrodaGateway"
    return Path.home() / ".local" / "share" / "IrodaGateway"


def _text(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _int(name: str, default: int) -> int:
    raw = _text(name)
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = _text(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on", "igen")


@dataclass(frozen=True)
class Config:
    server_url: str
    api_key: str
    gateway_id: str
    serial_port: str | None
    baud_rate: int
    tz_name: str
    data_dir: Path
    log_level: str
    env_enabled: bool
    version: str = VERSION
    tz: ZoneInfo = field(init=False)

    def __post_init__(self) -> None:
        try:
            zone = ZoneInfo(self.tz_name)
        except Exception:
            zone = ZoneInfo(DEFAULT_TZ)
        object.__setattr__(self, "tz", zone)

    # -- származtatott útvonalak -------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "queue.db"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def log_file(self) -> Path:
        return self.log_dir / "gateway.log"

    # -- végpontok ---------------------------------------------------------
    def url(self, path: str) -> str:
        return f"{self.server_url.rstrip('/')}/{path.lstrip('/')}"

    @property
    def events_url(self) -> str:
        return self.url("/api/v1/events")

    @property
    def env_url(self) -> str:
        return self.url("/api/v1/env")

    @property
    def heartbeat_url(self) -> str:
        return self.url("/api/v1/heartbeat")

    @property
    def health_url(self) -> str:
        return self.url("/api/v1/health")

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)


def load_config(env_file: Path | None = None) -> Config:
    """Beolvassa a `.env`-et és összeállítja a konfigurációt.

    A `.env` nem írja felül a már beállított környezeti változókat, így a
    szolgáltatás szintjén megadott érték erősebb.
    """
    path = env_file if env_file is not None else GATEWAY_DIR / ".env"
    if path.exists():
        load_dotenv(path, override=False)

    raw_data_dir = _text("DATA_DIR")
    data_dir = Path(raw_data_dir) if raw_data_dir else default_data_dir()

    return Config(
        server_url=_text("SERVER_URL", DEFAULT_SERVER_URL),
        api_key=_text("GATEWAY_API_KEY"),
        gateway_id=_text("GATEWAY_ID", DEFAULT_GATEWAY_ID),
        serial_port=_text("SERIAL_PORT") or None,
        baud_rate=_int("BAUD_RATE", DEFAULT_BAUD_RATE),
        tz_name=_text("TZ", DEFAULT_TZ),
        data_dir=data_dir,
        log_level=_text("LOG_LEVEL", "INFO").upper() or "INFO",
        env_enabled=_bool("ENV_ENABLED", True),
    )
