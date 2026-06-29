import os
import time
import queue
import logging
import threading
import collections
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from flask import Flask, request, render_template_string, jsonify, Response

# ----------------------------------------------------------------------------
# Konfiguráció
# ----------------------------------------------------------------------------
ORADIJ = 1900
PORT = int(os.environ.get("PORT", 5000))

# A naplót SQLite-ban tároljuk a /data volume-on. EZ AZ ALAP, nem kell hozzá
# semmilyen Railway-változó. (Ha valaha mégis adsz DATABASE_URL-t egy VALÓDI
# Postgreshez, automatikusan azt használja – de üres értéket figyelmen kívül hagy.)
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_PG = bool(DATABASE_URL)
DB_PATH = os.environ.get("DB_PATH", "/data/iroda.db")

PH = "%s" if USE_PG else "?"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("iroda")

if USE_PG:
    import psycopg2

app = Flask(__name__)

# ----------------------------------------------------------------------------
# Élő szenzoradat (csak memóriában, szálbiztosan)
# ----------------------------------------------------------------------------
_sensor_lock = threading.Lock()
latest_sensor_data = {"temp": "--", "hum": "--", "ido": "Nincs adat"}


# ----------------------------------------------------------------------------
# Adatbázis-kapcsolat
# ----------------------------------------------------------------------------
@contextmanager
def get_conn():
    if USE_PG:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
    else:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    try:
        cur = conn.cursor()
        if USE_PG:
            cur.execute("SET statement_timeout = 15000")
        else:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def init_db():
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            if USE_PG:
                cur.execute(
                    """CREATE TABLE IF NOT EXISTS naplo (
                        id SERIAL PRIMARY KEY,
                        nev TEXT NOT NULL, statusz TEXT NOT NULL, ido TEXT NOT NULL)"""
                )
            else:
                cur.execute(
                    """CREATE TABLE IF NOT EXISTS naplo (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        nev TEXT NOT NULL, statusz TEXT NOT NULL, ido TEXT NOT NULL)"""
                )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_naplo_nev ON naplo(nev)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_naplo_ido ON naplo(ido)")
            cur.close()
        log.info("Adatbázis kész: %s", "PostgreSQL" if USE_PG else f"SQLite ({DB_PATH})")
    except Exception as e:
        log.exception("init_db hiba: %s", e)


init_db()


# ----------------------------------------------------------------------------
# HÁTTÉR-ÍRÓ: az írás soha nem a webkérés szálán történik
# ----------------------------------------------------------------------------
# A /log csak beteszi a bejegyzést ebbe a sorba és azonnal visszatér.
# Egyetlen dedikált háttérszál végzi a tényleges INSERT-et. Emiatt:
#  - egyetlen webkérés sem akadhat be a lemezíráson -> a szerver NEM fagy le,
#  - az írások egy szálon, sorban mennek -> nincs "database is locked".
_write_q = queue.Queue()


def _writer_loop():
    while True:
        try:
            nev, statusz, ido = _write_q.get()
        except Exception:
            continue
        try:
            ok = False
            for attempt in range(5):
                try:
                    with get_conn() as conn:
                        cur = conn.cursor()
                        cur.execute(
                            f"INSERT INTO naplo (nev, statusz, ido) VALUES ({PH}, {PH}, {PH})",
                            (nev, statusz, ido),
                        )
                        cur.close()
                    log.info("Naplo MENTVE: %s %s @ %s", nev, statusz, ido)
                    ok = True
                    break
                except Exception as e:
                    log.warning("Író újrapróba (%d): %s", attempt + 1, e)
                    time.sleep(1)
            if not ok:
                log.error("Író: VÉGLEG nem sikerült menteni: %s %s @ %s", nev, statusz, ido)
        finally:
            _write_q.task_done()


_writer_thread = threading.Thread(target=_writer_loop, name="db-writer", daemon=True)
_writer_thread.start()


# ----------------------------------------------------------------------------
# Rugalmas kérés-feldolgozás (hardver-kompatibilitás)
# ----------------------------------------------------------------------------
def parse_payload():
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
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


# ----------------------------------------------------------------------------
# Endpointok
# ----------------------------------------------------------------------------
@app.route("/telemetry", methods=["POST"])
def telemetry():
    global latest_sensor_data
    adat = parse_payload()
    temp = pick(adat, "temp", "temperature", "hom", "homerseklet")
    hum = pick(adat, "hum", "humidity", "para", "paratartalom")
    if temp is None or hum is None:
        log.warning("Telemetry hiányos adat: %r", adat)
        return jsonify({"status": "error", "message": "Hiányzó temp vagy hum adat"}), 400
    with _sensor_lock:
        latest_sensor_data = {
            "temp": temp, "hum": hum, "ido": datetime.now().strftime("%H:%M:%S")
        }
    log.info("Telemetry: temp=%s hum=%s", temp, hum)
    return jsonify({"status": "ok"}), 200


@app.route("/latest", methods=["GET"])
def latest():
    with _sensor_lock:
        data = dict(latest_sensor_data)
    resp = jsonify(data)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/log", methods=["POST"])
def log_entry():
    adat = parse_payload()
    log.info("Log kérés érkezett: %r", adat)
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
    # Sorba tesszük és AZONNAL visszatérünk – nem várunk a lemezírásra.
    _write_q.put((nev, statusz, ido))
    _invalidate_cache()
    log.info("Log sorba téve: %s %s @ %s", nev, statusz, ido)
    return jsonify({"status": "ok"}), 200


@app.route("/health", methods=["GET"])
def health():
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
            cur.close()
        return jsonify({"status": "ok", "db": "ok", "queue": _write_q.qsize()}), 200
    except Exception as e:
        log.exception("Healthcheck hiba: %s", e)
        return jsonify({"status": "error", "db": "fail"}), 500


@app.route("/favicon.ico")
def favicon():
    return Response(status=204)


# ----------------------------------------------------------------------------
# Adatlekérés
# ----------------------------------------------------------------------------
def fetch_all_logs():
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT nev, statusz, ido FROM naplo ORDER BY nev ASC, ido ASC, id ASC")
            rows = cur.fetchall()
            cur.close()
        return rows
    except Exception as e:
        log.exception("Napló olvasási hiba: %s", e)
        return []


def fetch_last_per_user():
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT n.nev, n.statusz, n.ido FROM naplo n
                   INNER JOIN (SELECT nev, MAX(id) AS max_id FROM naplo GROUP BY nev) last_logs
                     ON n.nev = last_logs.nev AND n.id = last_logs.max_id
                   ORDER BY n.nev ASC"""
            )
            rows = cur.fetchall()
            cur.close()
        return rows
    except Exception as e:
        log.exception("Utolsó események olvasási hiba: %s", e)
        return []


# ----------------------------------------------------------------------------
# Munkaidő-számítás – automatikus kiléptetés éjfélkor
# ----------------------------------------------------------------------------
def _end_of_day(dt):
    return dt.replace(hour=23, minute=59, second=59, microsecond=0)


def _add_session(monthly_data, nev, start, end):
    if end <= start:
        return
    monthly_data[start.strftime("%Y-%m")][nev]["seconds"] += (end - start).total_seconds()


def calculate_stats_for_all_months():
    rows = fetch_all_logs()
    if not rows:
        return collections.OrderedDict()

    events_by_user = collections.defaultdict(list)
    for nev, statusz, ido_str in rows:
        try:
            ido = datetime.strptime(str(ido_str), "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue
        events_by_user[nev].append((ido, str(statusz).upper()))

    monthly_data = collections.defaultdict(
        lambda: collections.defaultdict(lambda: {"seconds": 0})
    )
    now = datetime.now()
    today = now.date()

    for nev, events in events_by_user.items():
        open_in = None
        for ido, statusz in events:
            if statusz == "BE":
                if open_in is not None:
                    if open_in.date() < ido.date():
                        _add_session(monthly_data, nev, open_in, _end_of_day(open_in))
                        open_in = ido
                else:
                    open_in = ido
            elif statusz == "KI":
                if open_in is None:
                    continue
                if open_in.date() == ido.date():
                    _add_session(monthly_data, nev, open_in, ido)
                    open_in = None
                else:
                    _add_session(monthly_data, nev, open_in, _end_of_day(open_in))
                    open_in = None
        if open_in is not None:
            if open_in.date() == today:
                _add_session(monthly_data, nev, open_in, now)
            else:
                _add_session(monthly_data, nev, open_in, _end_of_day(open_in))

    final_stats = {}
    for honap, nevek in monthly_data.items():
        final_stats[honap] = []
        for nev, adat in nevek.items():
            orak = adat["seconds"] / 3600
            final_stats[honap].append({
                "nev": nev, "ora": round(orak, 2), "fizetes": int(orak * ORADIJ)
            })
        final_stats[honap].sort(key=lambda x: x["nev"])
    return collections.OrderedDict(sorted(final_stats.items(), reverse=True))


def get_currently_inside():
    rows = fetch_last_per_user()
    today_str = datetime.now().strftime("%Y-%m-%d")
    inside = []
    for nev, statusz, ido in rows:
        if str(statusz).upper() == "BE" and str(ido).startswith(today_str):
            inside.append({"nev": nev, "ido": ido})
    return inside


# ----------------------------------------------------------------------------
# Dashboard adat gyorsítótár (max 5 mp-enként éri el a DB-t)
# ----------------------------------------------------------------------------
_cache_lock = threading.Lock()
_cache = {"ts": 0.0, "stats": None, "inside": None}


def _invalidate_cache():
    with _cache_lock:
        _cache["ts"] = 0.0


def get_dashboard_data():
    with _cache_lock:
        if _cache["stats"] is not None and (time.time() - _cache["ts"]) < 5:
            return _cache["stats"], _cache["inside"]
    stats = calculate_stats_for_all_months()
    inside = get_currently_inside()
    with _cache_lock:
        _cache.update(ts=time.time(), stats=stats, inside=inside)
    return stats, inside


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
        .live-card { background: linear-gradient(135deg, #007bff, #00d4ff); color: white; border-radius: 15px; }
        .stat-card { background: white; border-radius: 12px; border-left: 5px solid #28a745; transition: 0.3s; }
        .stat-card:hover { transform: translateY(-5px); }
        .salary { font-weight: bold; color: #198754; font-size: 1.2rem; }
        .month-card { background: white; border-radius: 12px; }
        .sensor-note { font-size: 0.9rem; opacity: 0.85; }
        #stale { display:none; }
    </style>
</head>
<body>
    <div class="container py-4">
        <div class="card live-card p-4 mb-4 shadow text-center">
            <div class="row align-items-center">
                <div class="col-md-4"><h4 id="temp">🌡️ {{ sensor.temp }}°C</h4><small>Hőmérséklet élőben</small></div>
                <div class="col-md-4"><h4 id="hum">💧 {{ sensor.hum }}%</h4><small>Páratartalom élőben</small></div>
                <div class="col-md-4"><h5 id="ido">🕒 {{ sensor.ido }}</h5><small>Utolsó frissítés</small></div>
            </div>
            <div class="mt-2"><span id="stale" class="badge bg-warning text-dark">⚠ Nincs friss szenzoradat – az eszköz épp nem küld</span></div>
            <div class="mt-3 sensor-note">A hőmérséklet és páratartalom csak élő kijelzés. Nem kerül adatbázisba, mindig csak az utolsó mérés van memóriában.</div>
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
                        <p class="mb-1 text-muted">Ledolgozott: <strong>{{ user.ora }} óra</strong></p>
                        <div class="salary">{{ "{:,}".format(user.fizetes) }} Ft</div>
                    </div>
                </div>
                {% endfor %}
            {% else %}
                <div class="col-12"><div class="alert alert-info shadow-sm">Ebben a hónapban még nincs elszámolt munkaidő.</div></div>
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
                <div class="alert alert-secondary shadow-sm">Még nincs korábbi havi archív adat.</div>
            {% endif %}
        </div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script>
        let lastIdo = null, lastChange = Date.now();
        async function frissitSensor() {
            try {
                const r = await fetch("/latest", { cache: "no-store" });
                if (!r.ok) return;
                const d = await r.json();
                document.getElementById("temp").innerText = "🌡️ " + d.temp + "°C";
                document.getElementById("hum").innerText = "💧 " + d.hum + "%";
                document.getElementById("ido").innerText = "🕒 " + d.ido;
                if (d.ido !== lastIdo) { lastIdo = d.ido; lastChange = Date.now(); }
                document.getElementById("stale").style.display = (Date.now() - lastChange) > 90000 ? "inline-block" : "none";
            } catch (e) { console.log("Szenzor hiba:", e); }
        }
        frissitSensor();
        setInterval(frissitSensor, 5000);
    </script>
</body>
</html>
"""


@app.route("/")
def index():
    all_monthly_stats, currently_inside = get_dashboard_data()
    current_month = datetime.now().strftime("%Y-%m")
    current_stats = all_monthly_stats.get(current_month, [])
    archive_stats = {k: v for k, v in all_monthly_stats.items() if k != current_month}
    with _sensor_lock:
        sensor = dict(latest_sensor_data)
    return render_template_string(
        HTML_TEMPLATE,
        sensor=sensor, current_month=current_month, current_stats=current_stats,
        archive_stats=archive_stats, currently_inside=currently_inside,
    )


# ----------------------------------------------------------------------------
# Indítás – éles szerver (waitress)
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    log.info("Iroda szerver indul a %d porton (waitress)...", PORT)
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=PORT, threads=8, channel_timeout=30)
    except ImportError:
        log.warning("waitress nem található, fejlesztői szerver indul.")
        app.run(host="0.0.0.0", port=PORT, threaded=True)
