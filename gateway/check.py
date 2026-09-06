"""Diagnosztika – ezt futtasd le először, ha valami nem működik.

    cd C:\\iroda-system\\gateway
    .venv\\Scripts\\python.exe check.py

Nem indít szolgáltatást és nem ír semmit az adatbázisba. Csak megnézi, hogy
mi van, és emberi nyelven leírja.

Fontos: a szolgáltatás futása közben a soros portot NEM lehet másodszor
megnyitni. Ha a szolgáltatás fut, a portos rész „foglalt” választ fog adni –
ez ilyenkor normális. Teljes vizsgálathoz állítsd le előbb:
`nssm stop RfidGateway`.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
import time
from pathlib import Path

GATEWAY_DIR = Path(__file__).resolve().parent
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

import db  # noqa: E402
import protocol  # noqa: E402
import serial_reader  # noqa: E402
from config import Config, load_config  # noqa: E402

OK = "[OK]  "
WARN = "[!]   "
FAIL = "[HIBA]"
INFO = "      "

# Ennyi ideig várunk az Arduino válaszára egy-egy parancs után.
ANSWER_TIMEOUT_SECONDS = 3.0
# Az ENV lekérdezésre tovább is várhat, ha épp mér.
ENV_TIMEOUT_SECONDS = 5.0


def cim(text: str) -> None:
    print()
    print(text)
    print("-" * len(text))


# --------------------------------------------------------------------------
# 1. Konfiguráció
# --------------------------------------------------------------------------
def ellenoriz_konfiguracio(cfg: Config) -> None:
    cim("1. Beállítások")
    env_file = GATEWAY_DIR / ".env"
    if env_file.exists():
        print(f"{OK} A .env fájl megvan: {env_file}")
    else:
        print(f"{FAIL} Nincs .env fájl itt: {env_file}")
        print(f"{INFO} Másold le a mintát:  copy .env.example .env   majd töltsd ki.")

    print(f"{INFO} gateway_id  : {cfg.gateway_id}")
    print(f"{INFO} szerver     : {cfg.server_url}")
    print(f"{INFO} soros port  : {cfg.serial_port or 'automatikus keresés'}")
    print(f"{INFO} adatkönyvtár: {cfg.data_dir}")
    print(f"{INFO} időzóna     : {cfg.tz_name}")
    print(f"{INFO} ENV gyűjtés : {'be' if cfg.env_enabled else 'ki'}")

    if cfg.api_key:
        print(f"{OK} Az API kulcs be van állítva ({len(cfg.api_key)} karakter).")
    else:
        print(f"{FAIL} A GATEWAY_API_KEY üres. A szerver minden feltöltést 401-gyel utasít vissza.")

    if cfg.data_dir.exists():
        print(f"{OK} Az adatkönyvtár létezik.")
    else:
        print(f"{WARN} Az adatkönyvtár még nincs meg, a szolgáltatás induláskor létrehozza.")


# --------------------------------------------------------------------------
# 2. Soros port
# --------------------------------------------------------------------------
def _olvass_valaszokat(port, masodperc: float) -> list[protocol.Line]:
    hatarido = time.time() + masodperc
    valaszok: list[protocol.Line] = []
    while time.time() < hatarido:
        try:
            nyers = port.readline()
        except Exception as exc:
            print(f"{FAIL} Olvasási hiba a portról: {exc}")
            break
        if not nyers:
            continue
        line = protocol.parse_line(nyers)
        if line is not None:
            valaszok.append(line)
    return valaszok


def _mit_valaszolt(valaszok: list[protocol.Line], fajta: str) -> protocol.Line | None:
    for line in valaszok:
        if line.kind == fajta:
            return line
    return None


def ellenoriz_soros_port(cfg: Config) -> None:
    cim("2. Soros port és Arduino")

    portok = serial_reader.available_ports()
    if portok:
        print(f"{INFO} A gépen látszó soros portok:")
        for port in portok:
            print(f"{INFO}   {serial_reader.describe_port(port)}")
    else:
        print(f"{WARN} A gép egyetlen soros portot sem lát.")
        print(f"{INFO} Nézd meg az Eszközkezelőben (Portok COM és LPT), hogy ott van-e az eszköz.")

    port_nev = serial_reader.resolve_port(cfg)
    if port_nev is None:
        print(f"{FAIL} Nem találtam használható portot.")
        print(f"{INFO} Ha tudod, melyik az, írd be a .env fájlba: SERIAL_PORT=COM5")
        return

    forras = "a .env-ből" if cfg.serial_port else "automatikus kereséssel"
    print(f"{OK} A használandó port {forras}: {port_nev}")

    try:
        port = serial_reader.open_port(cfg, port_nev)
    except Exception as exc:
        print(f"{FAIL} A port nem nyitható meg.")
        print(f"{INFO} {serial_reader.busy_port_message(port_nev, exc)}")
        print(f"{INFO} Ha a szolgáltatás fut, ez normális: nssm stop RfidGateway, majd futtasd újra.")
        return

    try:
        print(f"{OK} A {port_nev} port megnyitva, {cfg.baud_rate} baud.")

        # A port megnyitása általában újraindítja a Nanót, ilyenkor RDY-t küld.
        print(f"{INFO} Várok az eszköz indulási üzenetére...")
        indulas = _olvass_valaszokat(port, ANSWER_TIMEOUT_SECONDS)
        rdy = _mit_valaszolt(indulas, protocol.READY)
        if rdy is not None:
            print(f"{OK} Az eszköz elindult. Firmware: {rdy.firmware or '?'}")
            print(f"{INFO} RC522 olvasó: {rdy.fields.get('rc522', '?')}, "
                  f"BME280 szenzor: {rdy.fields.get('bme', '?')}")
            if rdy.bme_present is False:
                print(f"{WARN} A BME280 szenzor nincs meg – környezeti adat nem lesz.")
        else:
            print(f"{INFO} Nem jött indulási üzenet (ez nem baj, ha az eszköz már régóta fut).")

        # PING -> PONG
        port.write(f"{protocol.CMD_PING}\n".encode("ascii"))
        port.flush()
        pong = _mit_valaszolt(_olvass_valaszokat(port, ANSWER_TIMEOUT_SECONDS), protocol.PONG)
        if pong is not None:
            print(f"{OK} PING -> PONG megjött. Firmware: {pong.firmware or '?'}")
        else:
            print(f"{FAIL} A PING-re nem jött válasz {ANSWER_TIMEOUT_SECONDS:.0f} másodpercen belül.")
            print(f"{INFO} Ellenőrizd a baud rate-et (.env: BAUD_RATE) és a sketch verzióját.")

        # VER
        port.write(f"{protocol.CMD_VER}\n".encode("ascii"))
        port.flush()
        valaszok = _olvass_valaszokat(port, ANSWER_TIMEOUT_SECONDS)
        ver = _mit_valaszolt(valaszok, protocol.READY) or _mit_valaszolt(valaszok, protocol.PONG)
        if ver is not None:
            print(f"{OK} VER -> firmware: {ver.firmware or '?'}"
                  + (f", BME280: {ver.fields['bme']}" if "bme" in ver.fields else ""))
        else:
            print(f"{WARN} A VER parancsra nem jött értelmezhető válasz.")

        # ENV
        port.write(f"{protocol.CMD_ENV}\n".encode("ascii"))
        port.flush()
        env = _mit_valaszolt(_olvass_valaszokat(port, ENV_TIMEOUT_SECONDS), protocol.ENV)
        if env is not None:
            print(f"{OK} Aktuális mérés: {env.temp:.2f} °C, {env.hum:.2f} %, {env.press:.2f} hPa")
        else:
            print(f"{WARN} Az ENV lekérdezésre nem jött mérés.")
            print(f"{INFO} Ha ERR;BME_OFFLINE üzenetet látsz a naplóban, a szenzor kábelezését nézd meg.")
    finally:
        try:
            port.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# 3. Puffer
# --------------------------------------------------------------------------
def ellenoriz_puffer(cfg: Config) -> None:
    cim("3. Puffer (queue.db)")
    if not cfg.db_path.exists():
        print(f"{WARN} Még nincs adatbázis: {cfg.db_path}")
        print(f"{INFO} A szolgáltatás első indulásakor jön létre. Ez új telepítésnél normális.")
        return

    print(f"{INFO} Fájl: {cfg.db_path} ({cfg.db_path.stat().st_size / 1024:.0f} kB)")
    try:
        conn = db.connect(cfg.db_path)
    except sqlite3.Error as exc:
        print(f"{FAIL} Az adatbázis nem nyitható meg: {exc}")
        return

    try:
        varakozo_belyegzes = db.count_by_status(conn, db.PUNCH_TABLE, db.STATUS_PENDING)
        varakozo_meres = db.count_by_status(conn, db.ENV_TABLE, db.STATUS_PENDING)
        hibas_belyegzes = db.count_by_status(conn, db.PUNCH_TABLE, db.STATUS_ERROR)
        hibas_meres = db.count_by_status(conn, db.ENV_TABLE, db.STATUS_ERROR)
        feltoltott = db.count_by_status(conn, db.PUNCH_TABLE, db.STATUS_SENT)
        nyers = conn.execute(
            "SELECT COUNT(*) AS n FROM env_samples WHERE aggregated = 0"
        ).fetchone()["n"]

        print(f"{INFO} Feltöltésre vár : {varakozo_belyegzes} bélyegzés, {varakozo_meres} órás mérés")
        print(f"{INFO} Már feltöltve   : {feltoltott} bélyegzés (a 30 napnál régebbieket takarítjuk)")
        print(f"{INFO} Nyers minta     : {nyers} db vár összesítésre")

        if varakozo_belyegzes + varakozo_meres == 0:
            print(f"{OK} A puffer üres, minden fel van töltve.")
        elif varakozo_belyegzes + varakozo_meres < 50:
            print(f"{OK} A puffer normál méretű.")
        else:
            print(f"{WARN} Sok elem vár feltöltésre. Ha ez a szám folyamatosan nő, a szerver "
                  f"nem érhető el, vagy rossz az API kulcs. Nézd meg a naplót.")

        if hibas_belyegzes or hibas_meres:
            print(f"{WARN} A szerver által visszautasított sorok: {hibas_belyegzes} bélyegzés, "
                  f"{hibas_meres} mérés. Ezeket nem próbáljuk újra, és nem is töröljük.")
            for row in conn.execute(
                "SELECT ts_local, uid, last_error FROM punch_queue WHERE status = ? "
                "ORDER BY id DESC LIMIT 5",
                (db.STATUS_ERROR,),
            ):
                print(f"{INFO}   {row['ts_local']}  UID={row['uid']}  ok: {row['last_error']}")

        utolso = conn.execute(
            "SELECT ts_local, uid, status FROM punch_queue ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if utolso is not None:
            print(f"{INFO} Utolsó bélyegzés: {utolso['ts_local']} UID={utolso['uid']} "
                  f"({utolso['status']})")
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 4. Szerver
# --------------------------------------------------------------------------
def ellenoriz_szerver(cfg: Config) -> None:
    cim("4. Szerver elérhetősége")
    try:
        import requests
    except ImportError:
        print(f"{FAIL} A requests csomag nincs telepítve. Futtasd: pip install -r requirements.txt")
        return

    print(f"{INFO} GET {cfg.health_url}")
    try:
        response = requests.get(cfg.health_url, timeout=(5, 15))
    except requests.RequestException as exc:
        print(f"{FAIL} A szerver nem érhető el: {exc}")
        print(f"{INFO} Ellenőrizd az internetkapcsolatot és a SERVER_URL értékét a .env-ben.")
        return

    if response.status_code != 200:
        print(f"{FAIL} A szerver HTTP {response.status_code} választ adott.")
        if response.status_code == 503:
            print(f"{INFO} A szerver fut, de az adatbázisa nem elérhető. Ez a Railway oldalán hiba.")
        return

    try:
        body = response.json()
    except ValueError:
        print(f"{WARN} A válasz nem JSON: {response.text[:200]}")
        return

    print(f"{OK} A szerver él. Állapot: {body.get('status')}, adatbázis: {body.get('db')}, "
          f"verzió: {body.get('version')}")

    if not cfg.api_key:
        print(f"{WARN} API kulcs nélkül a feltöltést nem tudom kipróbálni.")
        return

    # Üres köteg: nem ír semmit, de megmutatja, jó-e a kulcs.
    try:
        proba = requests.post(
            cfg.events_url,
            json={"gateway_id": cfg.gateway_id, "events": []},
            headers={"X-API-Key": cfg.api_key},
            timeout=(5, 15),
        )
    except requests.RequestException as exc:
        print(f"{WARN} Az API kulcs próbája nem sikerült: {exc}")
        return

    if proba.status_code == 200:
        print(f"{OK} Az API kulcs jó, a szerver elfogadja a gateway-t.")
    elif proba.status_code in (401, 403):
        print(f"{FAIL} A szerver elutasította a kulcsot (HTTP {proba.status_code}).")
        print(f"{INFO} A .env GATEWAY_API_KEY értékének pontosan egyeznie kell a Railway-en "
              f"beállítottal.")
    else:
        print(f"{WARN} Váratlan válasz a kulcspróbára: HTTP {proba.status_code}")


def main() -> int:
    # A modulok saját naplóüzenetei itt csak zavarnának: ez a script emberi
    # olvasásra készül, és mindent maga ír ki.
    logging.basicConfig(level=logging.CRITICAL)

    print("=" * 70)
    print("Iroda RFID gateway – diagnosztika")
    print("=" * 70)

    cfg = load_config()
    ellenoriz_konfiguracio(cfg)
    ellenoriz_soros_port(cfg)
    ellenoriz_puffer(cfg)
    ellenoriz_szerver(cfg)

    print()
    print("=" * 70)
    print("Kész. A részletes napló:", cfg.log_file)
    print("=" * 70)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nMegszakítva.")
        sys.exit(1)
