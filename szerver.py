import os
import collections
import sqlite3
from datetime import datetime
from flask import Flask, request, render_template_string, jsonify

app = Flask(__name__)

ORADIJ = 1900

# Railway-en a Volume mount path legyen pontosan: /data
DB_PATH = "/data/iroda.db"

# Élő szenzoradat.
# Ez NEM adatbázisba ment, csak memóriában tartja az utolsó mérést.
# Új mérésnél mindig felülíródik, tehát csak 1 aktuális adat van tárolva.
latest_sensor_data = {
    "temp": "--",
    "hum": "--",
    "ido": "Nincs adat"
}


def get_db_connection():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = get_db_connection()
    c = conn.cursor()

    # Új adatbázisnál már csak a munkaidő naplózáshoz szükséges adatokat tároljuk.
    # Ha régi adatbázisod van temp/hum oszlopokkal, az sem baj:
    # az INSERT csak a nev/statusz/ido mezőket tölti.
    c.execute("""
        CREATE TABLE IF NOT EXISTS naplo (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nev TEXT NOT NULL,
            statusz TEXT NOT NULL,
            ido TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()


@app.route("/telemetry", methods=["POST"])
def telemetry():
    """
    Élő hőmérséklet/páratartalom fogadása.
    Nem mentjük adatbázisba, csak felülírjuk a latest_sensor_data változót.
    """
    global latest_sensor_data

    adat = request.get_json(silent=True)

    if not adat or "temp" not in adat or "hum" not in adat:
        return jsonify({
            "status": "error",
            "message": "Hiányzó temp vagy hum adat"
        }), 400

    latest_sensor_data = {
        "temp": adat["temp"],
        "hum": adat["hum"],
        "ido": datetime.now().strftime("%H:%M:%S")
    }

    return jsonify({"status": "ok"}), 200


@app.route("/latest", methods=["GET"])
def latest():
    """
    A böngésző ezt hívja pár másodpercenként.
    Így nem kell újratölteni az egész dashboardot, csak az élő szenzoradat frissül.
    """
    return jsonify(latest_sensor_data)


@app.route("/log", methods=["POST"])
def log():
    """
    Belépés/kilépés naplózása.
    Csak ez kerül adatbázisba:
    - név
    - státusz: BE vagy KI
    - időpont

    Hőmérsékletet és páratartalmat már nem tárolunk a naplóban.
    """
    adat = request.get_json(silent=True)

    if not adat or "nev" not in adat or "statusz" not in adat:
        return jsonify({
            "status": "error",
            "message": "Hiányzó nev vagy statusz adat"
        }), 400

    nev = str(adat["nev"]).strip()
    statusz = str(adat["statusz"]).strip().upper()

    if not nev:
        return jsonify({
            "status": "error",
            "message": "Üres név"
        }), 400

    if statusz not in ("BE", "KI"):
        return jsonify({
            "status": "error",
            "message": "A statusz csak BE vagy KI lehet"
        }), 400

    conn = get_db_connection()
    c = conn.cursor()

    c.execute(
        "INSERT INTO naplo (nev, statusz, ido) VALUES (?, ?, ?)",
        (nev, statusz, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    )

    conn.commit()
    conn.close()

    return jsonify({"status": "ok"}), 200


def add_seconds_split_by_month(monthly_data, nev, start, end):
    """
    Egy BE-KI időszakot hónapokra bontva ad hozzá az elszámoláshoz.
    Így a május 31. -> június 1. típusú munkaidő sem veszik el.
    """
    current = start

    while current < end:
        if current.month == 12:
            next_month = current.replace(
                year=current.year + 1,
                month=1,
                day=1,
                hour=0,
                minute=0,
                second=0,
                microsecond=0
            )
        else:
            next_month = current.replace(
                month=current.month + 1,
                day=1,
                hour=0,
                minute=0,
                second=0,
                microsecond=0
            )

        segment_end = min(end, next_month)
        honap_kulcs = current.strftime("%Y-%m")

        monthly_data[honap_kulcs][nev]["seconds"] += (
            segment_end - current
        ).total_seconds()

        current = segment_end


def calculate_stats_for_all_months():
    if not os.path.exists(DB_PATH):
        return collections.OrderedDict()

    conn = get_db_connection()
    c = conn.cursor()

    c.execute("SELECT nev, statusz, ido FROM naplo ORDER BY ido ASC")
    logs = c.fetchall()

    conn.close()

    monthly_data = collections.defaultdict(
        lambda: collections.defaultdict(lambda: {"seconds": 0})
    )

    # Fontos: nem hónaponként tároljuk a last_in-t.
    # Így hónapváltásnál nem veszik el az átnyúló munkamenet.
    last_in_by_user = {}

    for nev, statusz, ido_str in logs:
        try:
            ido = datetime.strptime(ido_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue

        statusz = str(statusz).upper()

        if statusz == "BE":
            # Ha valaki kétszer BE-t küld KI nélkül, a legutolsó BE számít.
            last_in_by_user[nev] = ido

        elif statusz == "KI" and nev in last_in_by_user:
            start = last_in_by_user[nev]
            end = ido

            if end > start:
                add_seconds_split_by_month(monthly_data, nev, start, end)

            del last_in_by_user[nev]

    final_stats = {}

    for honap, nevek in monthly_data.items():
        final_stats[honap] = []

        for nev, adat in nevek.items():
            orak = adat["seconds"] / 3600
            final_stats[honap].append({
                "nev": nev,
                "ora": round(orak, 2),
                "fizetes": int(orak * ORADIJ)
            })

        final_stats[honap].sort(key=lambda x: x["nev"])

    return collections.OrderedDict(sorted(final_stats.items(), reverse=True))


def get_currently_inside():
    if not os.path.exists(DB_PATH):
        return []

    conn = get_db_connection()
    c = conn.cursor()

    c.execute("""
        SELECT n.nev, n.statusz, n.ido
        FROM naplo n
        INNER JOIN (
            SELECT nev, MAX(id) AS max_id
            FROM naplo
            GROUP BY nev
        ) last_logs
        ON n.nev = last_logs.nev AND n.id = last_logs.max_id
        ORDER BY n.nev ASC
    """)

    rows = c.fetchall()
    conn.close()

    currently_inside = []

    for nev, statusz, ido in rows:
        if str(statusz).upper() == "BE":
            currently_inside.append({
                "nev": nev,
                "ido": ido
            })

    return currently_inside


@app.route("/")
def index():
    all_monthly_stats = calculate_stats_for_all_months()
    currently_inside = get_currently_inside()

    current_month = datetime.now().strftime("%Y-%m")
    current_stats = all_monthly_stats.get(current_month, [])
    archive_stats = {
        k: v for k, v in all_monthly_stats.items()
        if k != current_month
    }

    html_template = """
    <!DOCTYPE html>
    <html lang="hu">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Irodai Dashboard</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">

        <style>
            body {
                background-color: #f0f2f5;
            }

            .live-card {
                background: linear-gradient(135deg, #007bff, #00d4ff);
                color: white;
                border-radius: 15px;
            }

            .stat-card {
                background: white;
                border-radius: 12px;
                border-left: 5px solid #28a745;
                transition: 0.3s;
            }

            .stat-card:hover {
                transform: translateY(-5px);
            }

            .salary {
                font-weight: bold;
                color: #198754;
                font-size: 1.2rem;
            }

            .month-card {
                background: white;
                border-radius: 12px;
            }

            .sensor-note {
                font-size: 0.9rem;
                opacity: 0.85;
            }
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
                            <span>
                                {{ u.ora }} óra |
                                <strong>{{ "{:,}".format(u.fizetes) }} Ft</strong>
                            </span>
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
                    const response = await fetch("/latest", {
                        cache: "no-store"
                    });

                    if (!response.ok) {
                        return;
                    }

                    const data = await response.json();

                    document.getElementById("temp").innerText = "🌡️ " + data.temp + "°C";
                    document.getElementById("hum").innerText = "💧 " + data.hum + "%";
                    document.getElementById("ido").innerText = "🕒 " + data.ido;
                } catch (error) {
                    console.log("Szenzor frissítési hiba:", error);
                }
            }

            // Csak az élő szenzoradatot frissítjük, nem az egész oldalt.
            // Ez sokkal kisebb terhelés, mint a teljes location.reload().
            frissitSensor();
            setInterval(frissitSensor, 5000);
        </script>
    </body>
    </html>
    """

    return render_template_string(
        html_template,
        sensor=latest_sensor_data,
        current_month=current_month,
        current_stats=current_stats,
        archive_stats=archive_stats,
        currently_inside=currently_inside
    )


if __name__ == "__main__":
    init_db()

    # Railway dinamikus portkezelés
    port = int(os.environ.get("PORT", 5000))

    app.run(host="0.0.0.0", port=port)
