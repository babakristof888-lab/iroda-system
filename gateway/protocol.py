"""A soros protokoll parsolása.

Ez a modul szándékosan **tiszta**: nincs benne se soros port, se adatbázis, se
hálózat. Egy szöveges sor megy be, egy `Line` jön ki. Ezért teljes egészében
tesztelhető hardver nélkül.

A soros porton bármikor jöhet szemét – bekapcsoláskor, kábelmozgásra,
feszültségingadozásra –, ezért itt semmi nem dob kivételt. Ami nem értelmezhető,
az `INVALID` vagy `UNKNOWN` fajtájú sorként jön vissza, indoklással, és a hívó
csak debug szinten naplózza.

Az Arduino (sketch v1.1.0) sorai:

    CARD;04A1B2C3
    ENV;T=23.45;H=41.20;P=1013.25
    DUP;04A1B2C3
    RDY;fw=1.1.0;rc522=0x92;bme=0x76
    PONG;fw=1.1.0
    ERR;RC522_OFFLINE
    OK;RC522_RECOVERED
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from config import HUM_RANGE, PRESS_RANGE, TEMP_RANGE

# A sorok fajtái
CARD = "card"
ENV = "env"
DUP = "dup"
READY = "ready"
PONG = "pong"
ERROR = "error"
OK = "ok"
UNKNOWN = "unknown"
INVALID = "invalid"

# Az Arduino által küldhető parancsok
CMD_PING = "PING"
CMD_VER = "VER"
CMD_ENV = "ENV"

# Az RC522 hexa UID-ja. Megengedő minta: nem hexára szűkítünk, hogy egy későbbi
# firmware más UID-formátuma se vesszen el – de a soros szemetet kizárja.
_UID_PATTERN = re.compile(r"^[0-9A-Za-z:_.-]{2,64}$")

# Az ERR/OK kódok (RC522_OFFLINE, BME_RECOVERED, ...)
_CODE_PATTERN = re.compile(r"^[0-9A-Z_]{2,64}$")

# Az Arduino hibakódjai, amikre a heartbeat állapota épül
ERR_RC522_OFFLINE = "RC522_OFFLINE"
ERR_BME_OFFLINE = "BME_OFFLINE"
OK_RC522_RECOVERED = "RC522_RECOVERED"
OK_BME_RECOVERED = "BME_RECOVERED"


@dataclass(frozen=True)
class Line:
    """Egy értelmezett soros sor."""

    kind: str
    raw: str
    uid: str | None = None
    temp: float | None = None
    hum: float | None = None
    press: float | None = None
    code: str | None = None
    fields: dict[str, str] = field(default_factory=dict)
    reason: str | None = None

    @property
    def firmware(self) -> str | None:
        return self.fields.get("fw")

    def _chip_present(self, key: str) -> bool | None:
        """A `RDY;...;bme=0x76` mezőkből: van-e ott a chip.

        `None`, ha a firmware nem mondott semmit róla – ilyenkor nem tudunk
        következtetni, és nem is állítunk semmit.
        """
        raw = self.fields.get(key)
        if raw is None:
            return None
        text = raw.strip().lower()
        if text in ("", "none", "-", "no", "off", "false"):
            return False
        try:
            return int(text, 0) != 0
        except ValueError:
            return None

    @property
    def bme_present(self) -> bool | None:
        return self._chip_present("bme")

    @property
    def rc522_present(self) -> bool | None:
        return self._chip_present("rc522")


def decode_line(data: bytes) -> str:
    """Nyers bájtok -> szöveg. Sosem dob kivételt."""
    if isinstance(data, str):
        return data
    try:
        return data.decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover - a replace miatt gyakorlatilag lehetetlen
        return ""


def _clean(raw: str) -> str:
    """Sorvég, whitespace és a keretező nullbájtok levágása.

    A sor *belsejében* maradó vezérlőkaraktert nem távolítjuk el: az sérült
    átvitelt jelent, és inkább dobjuk el az egész sort, mint hogy egy
    elrontott UID-ot bélyegzésként könyveljünk el.
    """
    return raw.strip().strip("\x00").strip()


def _is_printable(text: str) -> bool:
    return all(ch >= " " and ch != "\x7f" for ch in text)


def _key_values(parts: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in parts:
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip().lower()
        if key:
            result[key] = value.strip()
    return result


def _in_range(value: float, bounds: tuple[float, float]) -> bool:
    return bounds[0] <= value <= bounds[1]


def _parse_env(text: str, parts: list[str]) -> Line:
    values = _key_values(parts[1:])
    numbers: dict[str, float] = {}
    for key, label in (("t", "hőmérséklet"), ("h", "páratartalom"), ("p", "légnyomás")):
        if key not in values:
            return Line(INVALID, raw=text, reason=f"hiányzó mező: {label}")
        try:
            numbers[key] = float(values[key])
        except (TypeError, ValueError):
            return Line(INVALID, raw=text, reason=f"nem szám a(z) {label} mezőben: {values[key]!r}")

    # Tartományon kívüli érték = hibás mérés. Eldobjuk, hogy ne rontsa el az
    # órás átlagot; a szerver amúgy is `out_of_range` hibát adna rá.
    for key, bounds, label in (
        ("t", TEMP_RANGE, "hőmérséklet"),
        ("h", HUM_RANGE, "páratartalom"),
        ("p", PRESS_RANGE, "légnyomás"),
    ):
        if not _in_range(numbers[key], bounds):
            return Line(
                INVALID,
                raw=text,
                reason=(
                    f"tartományon kívüli {label}: {numbers[key]} "
                    f"(érvényes: {bounds[0]}..{bounds[1]})"
                ),
            )

    return Line(ENV, raw=text, temp=numbers["t"], hum=numbers["h"], press=numbers["p"])


def _parse_uid_line(kind: str, text: str, parts: list[str]) -> Line:
    if len(parts) < 2:
        return Line(INVALID, raw=text, reason="csonka sor, hiányzik az UID")
    uid = parts[1].strip()
    if not _UID_PATTERN.match(uid):
        return Line(INVALID, raw=text, reason=f"értelmezhetetlen UID: {uid!r}")
    return Line(kind, raw=text, uid=uid)


def _parse_code_line(kind: str, text: str, parts: list[str]) -> Line:
    if len(parts) < 2:
        return Line(INVALID, raw=text, reason="csonka sor, hiányzik a kód")
    code = parts[1].strip().upper()
    if not _CODE_PATTERN.match(code):
        return Line(INVALID, raw=text, reason=f"értelmezhetetlen kód: {parts[1]!r}")
    return Line(kind, raw=text, code=code, fields=_key_values(parts[1:]))


def parse_line(raw: str | bytes) -> Line | None:
    """Egy soros sor értelmezése.

    `None`, ha a sor üres (nincs mit naplózni sem). Minden más esetben `Line`
    jön vissza – ismeretlen prefixnél `UNKNOWN`, hibás tartalomnál `INVALID`.
    """
    if raw is None:
        return None
    text = _clean(decode_line(raw) if isinstance(raw, bytes) else raw)
    if not text:
        return None
    if "�" in text:
        return Line(INVALID, raw=text, reason="sérült bájtok a sorban")
    if not _is_printable(text):
        return Line(INVALID, raw=text, reason="nem nyomtatható karakter a sorban")

    parts = text.split(";")
    prefix = parts[0].strip().upper()

    if prefix == "CARD":
        return _parse_uid_line(CARD, text, parts)
    if prefix == "DUP":
        return _parse_uid_line(DUP, text, parts)
    if prefix == "ENV":
        return _parse_env(text, parts)
    if prefix == "ERR":
        return _parse_code_line(ERROR, text, parts)
    if prefix == "OK":
        return _parse_code_line(OK, text, parts)
    if prefix == "RDY":
        return Line(READY, raw=text, fields=_key_values(parts[1:]))
    if prefix == "PONG":
        return Line(PONG, raw=text, fields=_key_values(parts[1:]))

    return Line(UNKNOWN, raw=text, reason=f"ismeretlen prefix: {prefix!r}")
