"""Az RFID gateway belépési pontja.

    Arduino Nano (RC522 + BME280)  --USB soros-->  EZ  --HTTPS-->  Railway

A gateway olvas, időbélyeget tesz rá, lemezre menti, feltölti. **Semmilyen
üzleti logika nincs benne**: nem dönti el, hogy belépés vagy kilépés, nem
tudja, kié a kártya, nem számol munkaidőt, nem értékeli a hőmérsékletet.
Egyetlen kivétel a környezeti mérések órás statisztikai összesítése – hogy a
csúcsértékek is megmaradjanak, lásd `aggregator.py`.

Három szál dolgozik, közöttük egy SQLite fájl:

* **SerialReader** – olvassa a portot, azonnal lemezre ír,
* **EnvAggregator** – óránként összesíti a nyers mintákat,
* **Uploader** – feltölt, heartbeatet küld, naponta takarít.

A fő szál felügyel: ha egy szál meghal, újraindítja. Semmilyen kivétel nem
szivároghat ki úgy, hogy a folyamat némán meghal.

Futtatás: `python -u main.py` (NSSM ezt indítja szolgáltatásként).
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from typing import Callable

import aggregator
import db
import serial_reader
import uploader
from config import Config, load_config
from logging_setup import setup_logging
from status import Status

log = logging.getLogger("gateway.main")

SUPERVISOR_TICK_SECONDS = 1.0
RESTART_DELAY_SECONDS = 5.0
# Az NSSM alapból nem vár sokáig a leállásra. Minden írás azonnal commitolva
# van, így egy erőszakos kilövés sem veszít adatot – de adjunk esélyt a
# szálaknak, hogy maguktól, tisztán zárjanak.
SHUTDOWN_JOIN_SECONDS = 5.0


def _log_startup(cfg: Config) -> None:
    log.info("=" * 70)
    log.info("Iroda RFID gateway %s indul", cfg.version)
    log.info("  gateway_id : %s", cfg.gateway_id)
    log.info("  szerver    : %s", cfg.server_url)
    log.info("  soros port : %s", cfg.serial_port or "automatikus keresés")
    log.info("  baud       : %d", cfg.baud_rate)
    log.info("  adatkönyvtár: %s", cfg.data_dir)
    log.info("  időzóna    : %s", cfg.tz_name)
    log.info("  ENV gyűjtés: %s", "be" if cfg.env_enabled else "ki")
    log.info("=" * 70)
    if not cfg.api_key:
        log.error(
            "A GATEWAY_API_KEY nincs beállítva a .env fájlban. A bélyegzések a "
            "pufferbe kerülnek, de a szerver 401-gyel utasítja vissza a feltöltést."
        )


def _install_signal_handlers(stop: threading.Event) -> None:
    """Ctrl+C és a Windows szolgáltatás-leállítás tiszta kezelése."""

    def handler(signum, _frame):  # noqa: ANN001
        log.info("Leállítási jelzés érkezett (%s), zárok mindent.", signum)
        stop.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            # Nem a fő szálon vagyunk, vagy a platform nem ismeri – nem baj.
            pass


def _thread_wrapper(name: str, body: Callable[[], None]) -> Callable[[], None]:
    """Egy szál sem halhat meg némán: ami kijön belőle, az naplóba kerül."""

    def wrapped() -> None:
        try:
            body()
        except Exception:
            log.exception("A(z) %s szál kivétellel leállt, a felügyelő újraindítja.", name)
        else:
            log.info("A(z) %s szál befejeződött.", name)

    return wrapped


def run_once(cfg: Config, stop: threading.Event) -> None:
    """Egy teljes futás: szálak indítása, felügyelet, tiszta leállás."""
    cfg.ensure_directories()

    # A séma létrehozása még a szálak előtt, hogy egy hibás útvonal azonnal
    # kiderüljön, ne szálanként háromszor.
    conn = db.connect(cfg.db_path)
    try:
        db.init_schema(conn)
        log.info("Puffer adatbázis: %s (%d elem vár feltöltésre)", cfg.db_path, db.queue_size(conn))
    finally:
        conn.close()

    status = Status()

    # A szálak ehhez a futáshoz tartozó, saját leállító jelzést kapnak. Ha a
    # felügyelő ciklus bármiért kilép (leállítás vagy váratlan hiba), ez a
    # jelzés biztosan lezárja őket – nem maradhat két SerialReader ugyanazon a
    # porton egy újraindítás után.
    local_stop = threading.Event()
    factories: dict[str, Callable[[], None]] = {
        "SerialReader": lambda: serial_reader.run(cfg, status, local_stop),
        "EnvAggregator": lambda: aggregator.run(cfg, local_stop),
        "Uploader": lambda: uploader.run(cfg, status, local_stop),
    }

    threads: dict[str, threading.Thread] = {}
    try:
        for name, body in factories.items():
            thread = threading.Thread(target=_thread_wrapper(name, body), name=name, daemon=True)
            thread.start()
            threads[name] = thread

        while not stop.is_set():
            for name, thread in list(threads.items()):
                if thread.is_alive():
                    continue
                log.error("A(z) %s szál nem fut, újraindítom.", name)
                restarted = threading.Thread(
                    target=_thread_wrapper(name, factories[name]), name=name, daemon=True
                )
                restarted.start()
                threads[name] = restarted
            stop.wait(SUPERVISOR_TICK_SECONDS)
    finally:
        local_stop.set()
        log.info("Leállás: várok a szálakra...")
        for name, thread in threads.items():
            thread.join(timeout=SHUTDOWN_JOIN_SECONDS)
            if thread.is_alive():
                log.warning("A(z) %s szál nem állt le időben, daemonként elengedem.", name)
        log.info("A gateway leállt.")


def main() -> int:
    cfg = load_config()
    try:
        cfg.ensure_directories()
    except OSError as exc:
        # Még nincs naplófájl – legalább a stdouton látszódjon.
        print(f"Nem tudom létrehozni az adatkönyvtárat ({cfg.data_dir}): {exc}", file=sys.stderr)
        setup_logging(None, cfg.log_level)
    else:
        setup_logging(cfg.log_file, cfg.log_level)

    _log_startup(cfg)

    stop = threading.Event()
    _install_signal_handlers(stop)

    while not stop.is_set():
        try:
            run_once(cfg, stop)
        except KeyboardInterrupt:
            log.info("Ctrl+C – leállok.")
            stop.set()
        except Exception:
            # Ide semmi nem juthat el normál működésben. Ha mégis: naplózzuk,
            # várunk, és újrakezdjük. A folyamat nem halhat meg némán.
            log.exception("Váratlan hiba a fő ciklusban, %.0f másodperc múlva újraindulok.",
                          RESTART_DELAY_SECONDS)
            if stop.wait(RESTART_DELAY_SECONDS):
                break
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
