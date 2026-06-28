import os
import time
import logging
import threading
import collections
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

from flask import Flask, request, render_template_string, jsonify, Response

# ----------------------------------------------------------------------------
# Konfiguráció
# ----------------------------------------------------------------------------
ORADIJ = 1900

# Railway-en a Volume mount path legyen pontosan: /data
# (Felülírható a DB_PATH környezeti változóval, pl. helyi teszthez.)
DB_PATH = os.environ.get("DB_PATH", "/data/iroda.db")
PORT = int(os.environ.get("PORT", 5000))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("iroda")

app = Flask(__name__)

# ----------------------------------------------------------------------------
# Élő szenzoradat (csak memóriában, szálbiztosan)
# ----------------------------------------------------------------------------
# Ez NEM kerül adatbázisba, csak az utolsó mérést tartja memóriában.
# Szerver-újraindításkor visszaáll "--"-ra, amíg új mérés nem érkezik.
# A waitress több szálon szolgál ki, ezért lock védi az írást/olvasást.
_sensor_lock = threading.Lock()
latest_sensor_data = {
    "temp": "--",
    "hum": "--",
    "ido": "Nincs adat",
}


# ----------------------------------------------------------------------------
# Adatbázis – robusztus SQLite kapcsolat
# ----------------------------------------------------------------------------
@contextmanager
def get_db():
    """
    SQLite kapcsolat WAL módban, hosszú busy_timeout-tal.
    Így a párhuzamos olvasás/írás nem dob "database is locked" hibát,
    és minden kapcsolat garantáltan lezárul.
    """
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Létrehozza a táblát és az indexeket, ha még nincsenek."""
    try:
        with get_db() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS naplo (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    nev TEXT NOT NULL,
                    statusz TEXT NOT NULL,
                    ido TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_naplo_nev ON naplo(nev);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_naplo_ido ON naplo(ido);")
        log.info("Adatbázis kész: %s", DB_PATH)
    except Exception as e:
        # Ne álljon le az import emiatt – az endpointok külön kezelik a hibát.
        log.exception("Nem sikerült inicializálni az adatbázist: %s", e)


# Fontos: az init az import során fut, nem csak a __main__ blokkban.
# Így akkor is működik, ha a szervert nem 'python app.py'-vel indítják.
init_db()


# ----------------------------------------------------------------------------
# Rugalmas kérés-feldolgozás (hardver-kompatibilitás)
# ----------------------------------------------------------------------------
def parse_payload():
    """
    Visszaadja a kérés adatait dict-ként, akárhogy is küldte a hardver:
    - application/json
    - application/x-www-form-urlencoded (HTML form / sok Arduino-kliens)
    - query string (?temp=..&hum=..)

    Az ESP/Arduino kliensek gyakran NEM állítják be a
    'Content-Type: application/json' fejlécet, ezért a sima get_json()
    None-t adna vissza és 400-as hibát kapnál. Ez a függvény ezt megoldja.
    """
    data = request.get_json(silent=True)
    if isinstance(data, dict) and data:
        return data

    merged = {}
    if request.form:
        merged.update(request.form.to_dict())
    if request.args:
        merged.update(request.args.to_dict())
    if merged:
        return merged

    # Végső eset: nyers body kézi JSON-ként
    raw = request.get_data(as_text=True)
    if raw:
        import json
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {}


def pick(d, *keys):
    """Több lehetséges kulcsnév közül az elsőt adja vissza, ami létezik."""
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


# ----------------------------------------------------------------------------
# Endpointok
# ----------------------------------------------------------------------------
@app.route("/telemetry", methods=["POST"])
def telemetry():
    """Élő hőmérséklet/páratartalom. Nem mentjük adatbázisba."""
    global latest_sensor_data
    adat = parse_payload()
    temp = pick(adat, "temp", "temperature", "hom", "homerseklet")
    hum = pick(adat, "hum", "humidity", "para", "paratartalom")

    if temp is None or hum is None:
        log.warning("Telemetry hiányos adat: %r", adat)
        return jsonify({"status": "error", "message": "Hiányzó temp vagy hum adat"}), 400

    with _sensor_lock:
        latest_sensor_data = {
            "temp": temp,
            "hum": hum,
            "ido": datetime.now().strftime("%H:%M:%S"),
        }
    log.info("Telemetry: temp=%s hum=%s", temp, hum)
    return jsonify({"status": "ok"}), 200


@app.route("/latest", methods=["GET"])
def latest():
    """A böngésző pár másodpercenként ezt kéri le az élő kijelzéshez."""
    with _sensor_lock:
        data = dict(latest_sensor_data)
    resp = jsonify(data)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/log", methods=["POST"])
def log_entry():
    """Belépés/kilépés naplózása. Csak ez kerül adatbázisba."""
    adat = parse_payload()
    nev = pick(adat, "nev", "name", "user")
    statusz = pick(adat, "statusz", "status", "state")

    if nev is None or statusz is None:
        log.warning("Log hiányos adat: %r", adat)
        return jsonify({"status": "error", "message": "Hiányzó nev vagy statusz adat"}), 400

    nev = str(nev).strip()
    statusz = str(statusz).strip().upper()

    if not nev:
        return jsonify({"status": "error", "message": "Üres név"}), 400
    if statusz not in ("BE", "KI"):
        return jsonify({"status": "error", "message": "A statusz csak BE vagy KI lehet"}), 400

    ido = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Rövid újrapróbálkozás zárolás esetére (a WAL mellett szinte sosem kell).
    for attempt in range(3):
        try:
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO naplo (nev, statusz, ido) VALUES (?, ?, ?)",
                    (nev, statusz, ido),
                )
            log.info("Naplo: %s %s @ %s", nev, statusz, ido)
            return jsonify({"status": "ok"}), 200
        except sqlite3.OperationalError as e:
            log.warning("DB írás újrapróbálás (%d): %s", attempt + 1, e)
            time.sleep(0.3)
        except Exception as e:
            log.exception("DB írási hiba: %s", e)
            return jsonify({"status": "error", "message": "Adatbázis hiba"}), 500

    return jsonify({"status": "error", "message": "Adatbázis foglalt"}), 503


@app.route("/health", methods=["GET"])
def health():
    """Egyszerű állapot-ellenőrzés (Railway healthcheck-hez is jó)."""
    try:
        with get_db() as conn:
            conn.execute("SELECT 1")
        return jsonify({"status": "ok", "db": "ok"}), 200
    except Exception as e:
        log.exception("Healthcheck DB hiba: %s", e)
        return jsonify({"status": "error", "db": "fail"}), 500


@app.route("/favicon.ico")
def favicon():
    # Csendes 204, hogy ne legyen tele a log 404-gyel.
    return Response(status=204)


# ----------------------------------------------------------------------------
# Munkaidő-számítás – automatikus kiléptetés éjfélkor
# ----------------------------------------------------------------------------
def _end_of_day(dt):
    """Az adott nap 23:59:59 időpontja."""
    return dt.replace(hour=23, minute=59, second=59, microsecond=0)


def _add_session(monthly_data, nev, start, end):
    """
    Egy lezárt munkamenetet ([start, end]) hozzáad az elszámoláshoz.
    Az automatikus éjféli kiléptetés miatt egy munkamenet egy napon belül van,
    de a hónapkulcsot a start alapján képezzük.
    """
    if end <= start:
        return
    honap_kulcs = start.strftime("%Y-%m")
    monthly_data[honap_kulcs][nev]["seconds"] += (end - start).total_seconds()


def calculate_stats_for_all_months():
    """
    Végigmegy a naplón felhasználónként, és összeszámolja a ledolgozott időt.

    Szabályok (automatikus éjféli kiléptetés):
    - Egy BE-hez tartozó KI ugyanazon a napon: normál lezárás.
    - Ha valaki BE után nem küld aznap KI-t (átnyúlik a következő napra,
      vagy egyáltalán nincs KI): a munkamenet automatikusan lezárul aznap
      23:59:59-kor. Így senki nem marad napokig "bent", és az óra beszámít.
    - A ma még nyitva lévő munkamenet a jelenlegi időpontig számít be.
    """
    if not os.path.exists(DB_PATH):
        return collections.OrderedDict()

    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT nev, statusz, ido FROM naplo ORDER BY nev ASC, ido ASC, id ASC"
            ).fetchall()
    except Exception as e:
        log.exception("Statisztika olvasási hiba: %s", e)
        return collections.OrderedDict()

    # Felhasználónként csoportosított, időrendezett események.
    events_by_user = collections.defaultdict(list)
    for nev, statusz, ido_str in rows:
        try:
            ido = datetime.strptime(ido_str, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue
        events_by_user[nev].append((ido, str(statusz).upper()))

    monthly_data = collections.defaultdict(
        lambda: collections.defaultdict(lambda: {"seconds": 0})
    )
    now = datetime.now()
    today = now.date()

    for nev, events in events_by_user.items():
        open_in = None  # nyitott BE időpontja
        for ido, statusz in events:
            if statusz == "BE":
                if open_in is not None:
                    # Volt már nyitott BE KI nélkül.
                    if open_in.date() < ido.date():
                        # Másik napról maradt nyitva -> éjféli auto-kiléptetés.
                        _add_session(monthly_data, nev, open_in, _end_of_day(open_in))
                        open_in = ido
                    # Ha ugyanaznap dupla BE: az elsőt tartjuk meg, a duplikátumot eldobjuk.
                else:
                    open_in = ido
            elif statusz == "KI":
                if open_in is None:
                    continue  # árva KI -> figyelmen kívül
                if open_in.date() == ido.date():
                    _add_session(monthly_data, nev, open_in, ido)
                    open_in = None
                else:
                    # A KI másik napon van -> az eredeti napot éjfélkor zárjuk,
                    # a kései KI-t eldobjuk.
                    _add_session(monthly_data, nev, open_in, _end_of_day(open_in))
                    open_in = None

        # A ciklus után még nyitva lévő munkamenet kezelése.
        if open_in is not None:
            if open_in.date() == today:
                _add_session(monthly_data, nev, open_in, now)  # ma még bent van
            else:
                _add_session(monthly_data, nev, open_in, _end_of_day(open_in))

    final_stats = {}
    for honap, nevek in monthly_data.items():
        final_stats[honap] = []
        for nev, adat in nevek.items():
            orak = adat["seconds"] / 3600
            final_stats[honap].append({
                "nev": nev,
                "ora": round(orak, 2),
                "fizetes": int(orak * ORADIJ),
            })
        final_stats[honap].sort(key=lambda x: x["nev"])

    return collections.OrderedDict(sorted(final_stats.items(), reverse=True))


def get_currently_inside():
    """
    Most bent lévők: akinek a legutolsó eseménye BE ÉS az a mai napon történt.
    A korábbi napról nyitva maradt BE-ket az auto-kiléptetés lezárja,
    ezért azok már nem jelennek meg "bent" állapotban.
    """
    if not os.path.exists(DB_PATH):
        return []

    try:
        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT n.nev, n.statusz, n.ido
                FROM naplo n
                INNER JOIN (
                    SELECT nev, MAX(id) AS max_id
                    FROM naplo
                    GROUP BY nev
                ) last_logs
                  ON n.nev = last_logs.nev AND n.id = last_logs.max_id
                ORDER BY n.nev ASC
                """
            ).fetchall()
    except Exception as e:
        log.exception("Bent lévők olvasási hiba: %s", e)
        return []

    today_str = datetime.now().strftime("%Y-%m-%d")
    currently_inside = []
    for nev, statusz, ido in rows:
        if str(statusz).upper() == "BE" and str(ido).startswith(today_str):
            currently_inside.append({"nev": nev, "ido": ido})
    return currently_inside


# ----------------------------------------------------------------------------
# Dashboard
# ----------------------------------------------------------------------------
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="hu">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Irodai Dashboard</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body { background-color: #f0f2f5; }
        .live-card {
            background: linear-gradient(135deg, #007bff, #00d4ff);
            color: white; border-radius: 15px;
        }
        .stat-card {
            background: white; border-radius: 12px;
            border-left: 5px solid #28a745; transition: 0.3s;
        }
        .stat-card:hover { transform: translateY(-5px); }
        .salary { font-weight: bold; color: #198754; font-size: 1.2rem; }
        .month-card { background: white; border-radius: 12px; }
        .sensor-note { font-size: 0.9rem; opacity: 0.85; }
    </style>
</head>
<body>
    <div class="container py-4">
        <div class="card live-card p-4 mb-4 shadow text-center">
            <div class="row align-items-center">
                <div class="col-md-4">
                    <h4 id="temp">🌡️ {{ sensor.temp }}°C</h4>
                    <small>Hőmérséklet élőben</small>
                </div>
                <div class="col-md-4">
                    <h4 id="hum">💧 {{ sensor.hum }}%</h4>
                    <small>Páratartalom élőben</small>
                </div>
                <div class="col-md-4">
                    <h5 id="ido">🕒 {{ sensor.ido }}</h5>
                    <small>Utolsó frissítés</small>
                </div>
            </div>
            <div class="mt-3 sensor-note">
                A hőmérséklet és páratartalom csak élő kijelzés.
                Nem kerül adatbázisba, mindig csak az utolsó mérés van memóriában.
            </div>
        </div>

        <div class="card p-4 mb-4 shadow">
            <h3 class="mb-3">👥 Jelenleg bent vannak</h3>
            {% if currently_inside %}
                <div class="row">
                    {% for user in currently_inside %}
                    <div class="col-md-4 mb-3">
                        <div class="card p-3 shadow-sm border-success">
                            <h5 class="text-success mb-1">{{ user.nev }}</h5>
                            <div><strong>✅ BENT</strong></div>
                            <div class="text-muted small">Belépés ideje: {{ user.ido }}</div>
                        </div>
                    </div>
                    {% endfor %}
                </div>
            {% else %}
                <p class="text-muted mb-0">Jelenleg senki nincs bent.</p>
            {% endif %}
        </div>

        <h2 class="mb-3">Aktuális hónap elszámolása ({{ current_month }})</h2>
        <div class="row mb-5">
            {% if current_stats %}
                {% for user in current_stats %}
                <div class="col-md-4 mb-3">
                    <div class="card p-3 stat-card shadow-sm">
                        <h5>{{ user.nev }}</h5>
                        <p class="mb-1 text-muted">
                            Ledolgozott: <strong>{{ user.ora }} óra</strong>
                        </p>
                        <div class="salary">{{ "{:,}".format(user.fizetes) }} Ft</div>
                    </div>
                </div>
                {% endfor %}
            {% else %}
                <div class="col-12">
                    <div class="alert alert-info shadow-sm">
                        Ebben a hónapban még nincs elszámolt munkaidő.
                        A korábbi hónapokat lent, az archívumban találod.
                    </div>
                </div>
            {% endif %}
        </div>

        <button class="btn btn-warning w-100 mb-4" type="button" data-bs-toggle="collapse" data-bs-target="#archive">
            📦 Korábbi hónapok / Archívum megnyitása-bezárása
        </button>
        <div class="collapse show" id="archive">
            {% if archive_stats %}
                {% for honap, adatok in archive_stats.items() %}
                <div class="card p-3 mb-3 shadow-sm month-card">
                    <h5 class="mb-3">📅 {{ honap }}</h5>
                    {% for u in adatok %}
                    <div class="d-flex justify-content-between small border-bottom py-2">
                        <span>{{ u.nev }}</span>
                        <span>{{ u.ora }} óra | <strong>{{ "{:,}".format(u.fizetes) }} Ft</strong></span>
                    </div>
                    {% endfor %}
                </div>
                {% endfor %}
            {% else %}
                <div class="alert alert-secondary shadow-sm">
                    Még nincs korábbi havi archív adat.
                </div>
            {% endif %}
        </div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script>
        async function frissitSensor() {
            try {
                const response = await fetch("/latest", { cache: "no-store" });
                if (!response.ok) return;
                const data = await response.json();
                document.getElementById("temp").innerText = "🌡️ " + data.temp + "°C";
                document.getElementById("hum").innerText = "💧 " + data.hum + "%";
                document.getElementById("ido").innerText = "🕒 " + data.ido;
            } catch (error) {
                console.log("Szenzor frissítési hiba:", error);
            }
        }
        frissitSensor();
        setInterval(frissitSensor, 5000);
    </script>
</body>
</html>
"""


@app.route("/")
def index():
    all_monthly_stats = calculate_stats_for_all_months()
    currently_inside = get_currently_inside()
    current_month = datetime.now().strftime("%Y-%m")
    current_stats = all_monthly_stats.get(current_month, [])
    archive_stats = {k: v for k, v in all_monthly_stats.items() if k != current_month}

    with _sensor_lock:
        sensor = dict(latest_sensor_data)

    return render_template_string(
        HTML_TEMPLATE,
        sensor=sensor,
        current_month=current_month,
        current_stats=current_stats,
        archive_stats=archive_stats,
        currently_inside=currently_inside,
    )


# ----------------------------------------------------------------------------
# Indítás – éles (production) szerverrel, NEM a Flask fejlesztői szerverrel
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    log.info("Iroda szerver indul a %d porton (waitress)...", PORT)
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=PORT, threads=8)
    except ImportError:
        # Ha valamiért nincs waitress telepítve, essen vissza a beépített szerverre.
        log.warning("waitress nem található, fejlesztői szerver indul (nem ajánlott élesben).")
        app.run(host="0.0.0.0", port=PORT, threaded=True)
