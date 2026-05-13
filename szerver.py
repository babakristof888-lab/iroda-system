import os
from flask import Flask, request, render_template_string
import sqlite3
from datetime import datetime
import collections

app = Flask(__name__)
ORADIJ = 1900

# FONTOS: A felhőben egy fix mappába mentünk, hogy ne vesszenek el az adatok!
DB_PATH = '/data/iroda.db'

latest_sensor_data = {"temp": "--", "hum": "--", "ido": "Nincs adat"}

def init_db():
    # Létrehozzuk a mappát, ha még nem létezne
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS naplo 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, nev TEXT, statusz TEXT, temp REAL, hum REAL, ido TEXT)''')
    conn.commit()
    conn.close()

@app.route('/telemetry', methods=['POST'])
def telemetry():
    global latest_sensor_data
    adat = request.json
    latest_sensor_data = {
        "temp": adat['temp'],
        "hum": adat['hum'],
        "ido": datetime.now().strftime("%H:%M:%S")
    }
    return {"status": "ok"}, 200

@app.route('/log', methods=['POST'])
def log():
    adat = request.json
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO naplo (nev, statusz, temp, hum, ido) VALUES (?, ?, ?, ?, ?)",
              (adat['nev'], adat['statusz'], adat['temp'], adat['hum'], datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()
    return {"status": "ok"}, 200

def calculate_stats_for_all_months():
    if not os.path.exists(DB_PATH):
        return {}
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT nev, statusz, ido FROM naplo ORDER BY ido ASC")
    logs = c.fetchall()
    conn.close()
    monthly_data = collections.defaultdict(lambda: collections.defaultdict(lambda: {'seconds': 0, 'last_in': None}))
    for nev, statusz, ido_str in logs:
        ido = datetime.strptime(ido_str, "%Y-%m-%d %H:%M:%S")
        honap_kulcs = ido.strftime("%Y-%m")
        user_month = monthly_data[honap_kulcs][nev]
        if statusz == "BE": user_month['last_in'] = ido
        elif statusz == "KI" and user_month['last_in']:
            delta = ido - user_month['last_in']
            user_month['seconds'] += delta.total_seconds()
            user_month['last_in'] = None
    final_stats = {}
    for honap, nevek in monthly_data.items():
        final_stats[honap] = []
        for nev, adat in nevek.items():
            orak = adat['seconds'] / 3600
            final_stats[honap].append({'nev': nev, 'ora': round(orak, 2), 'fizetes': int(orak * ORADIJ)})
    return collections.OrderedDict(sorted(final_stats.items(), reverse=True))

@app.route('/')
def index():
    all_monthly_stats = calculate_stats_for_all_months()
    current_month = datetime.now().strftime("%Y-%m")
    current_stats = all_monthly_stats.get(current_month, [])
    archive_stats = {k: v for k, v in all_monthly_stats.items() if k != current_month}

    html_template = """
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
        </style>
        <script> setInterval(function(){ location.reload(); }, 30000); </script> </head>
    <body>
        <div class="container py-4">
            <div class="card live-card p-4 mb-4 shadow text-center">
                <div class="row align-items-center">
                    <div class="col-md-4"><h4>🌡️ {{ sensor.temp }}°C</h4><small>Hőmérséklet</small></div>
                    <div class="col-md-4"><h4>💧 {{ sensor.hum }}%</h4><small>Páratartalom</small></div>
                    <div class="col-md-4"><h5>🕒 {{ sensor.ido }}</h5><small>Utolsó frissítés</small></div>
                </div>
            </div>
            <h2 class="mb-3">Aktuális hónap elszámolása ({{ current_month }})</h2>
            <div class="row mb-5">
                {% for user in current_stats %}
                <div class="col-md-4 mb-3">
                    <div class="card p-3 stat-card shadow-sm">
                        <h5>{{ user.nev }}</h5>
                        <p class="mb-1 text-muted">Ledolgozott: <strong>{{ user.ora }} óra</strong></p>
                        <div class="salary">{{ "{:,}".format(user.fizetes) }} Ft</div>
                    </div>
                </div>
                {% endfor %}
            </div>
            <button class="btn btn-outline-secondary w-100 mb-4" type="button" data-bs-toggle="collapse" data-bs-target="#archive">Archívum megnyitása</button>
            <div class="collapse" id="archive">
                {% for honap, adatok in archive_stats.items() %}
                <div class="card p-3 mb-2 shadow-sm">
                    <strong>{{ honap }}</strong>
                    {% for u in adatok %}
                    <div class="d-flex justify-content-between small border-bottom py-1">
                        <span>{{ u.nev }}</span><span>{{ u.ora }} óra | <strong>{{ "{:,}".format(u.fizetes) }} Ft</strong></span>
                    </div>
                    {% endfor %}
                </div>
                {% endfor %}
            </div>
        </div>
        <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    </body>
    </html>
    """
    return render_template_string(html_template, sensor=latest_sensor_data, current_month=current_month, current_stats=current_stats, archive_stats=archive_stats)

if __name__ == '__main__':
    init_db()
    # A Railway-nek szüksége van erre a dinamikus port kezelésre:
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)