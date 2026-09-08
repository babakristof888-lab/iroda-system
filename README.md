# Iroda – RFID munkaidő-nyilvántartó

Kártyás beléptetésen alapuló munkaidő-nyilvántartás egy irodához, környezeti
(hőmérséklet / páratartalom / légnyomás) méréssel kiegészítve.

```
Arduino Nano (RFID olvasó + BME280 + buzzer, nincs hálózat)
    │ USB soros, 115200 baud
    ▼
Windows mini PC (Python gateway, offline SQLite puffer)
    │ HTTPS POST
    ▼
Railway: FastAPI + SQLite + admin HTML felület     ←  EZ A REPÓ (server/)
```

A gateway csak továbbít. **Minden üzleti logika a szerveren fut**: a gateway nem
tudja és nem is dönti el, hogy egy bélyegzés belépés vagy kilépés.

## Repó felépítése

```
server/
  app/            az alkalmazás (API, admin felület, üzleti logika)
  tests/          pytest tesztek
  scripts/        segédscriptek (jelszó-hash)
  requirements.txt
  railway.json
  .env.example
```

Az `arduino/` és a `gateway/` külön munka, ez a repórész nem nyúl hozzájuk.

## Railway beállítás

A service neve `gleaming-learning`, a projekté „Iroda szerver”.

1. **Root Directory: `server`** — ez a legfontosabb. A Railway a repó gyökerében
   nem találja meg az alkalmazást. Service → Settings → Source → Root Directory.
2. **Builder:** Railpack (a `server/railway.json` ezt kéri).
3. **Volume:** a persistent volume mountpointja legyen `/data`. Az adatbázis
   egyetlen SQLite fájl ezen belül. Postgres nem kell, és nincs is a projektben.
4. **Healthcheck:** `/api/v1/health` (a `railway.json` beállítja).
5. **Start parancs:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
   (szintén a `railway.json`-ban).

A táblákat az induláskori migráció hozza létre, ha még nem léteznek — nincs
külön migrációs lépés. Ha a `/data` nem írható, a log egy érthető magyar
üzenetet ír ki arról, hogy mit kell beállítani, és a healthcheck 503-at ad.

## Környezeti változók

| Változó | Kötelező | Default | Mire való |
|---|---|---|---|
| `GATEWAY_API_KEY` | **igen** | – | A gateway ezzel hitelesít az `X-API-Key` fejlécben. Amíg nincs beállítva, minden gateway-hívás 401-et kap. |
| `ADMIN_PASSWORD_HASH` | **igen** | – | Az admin jelszó bcrypt hash-e. Amíg nincs beállítva, nem lehet belépni a felületre. |
| `DB_PATH` | igen | `/data/iroda.db` | Az SQLite fájl helye. Railway-en a volume mountpointján belül. Lokálisan `./dev.db`. |
| `TZ` | igen | `Europe/Budapest` | A megjelenítés és a naptári logika időzónája. Az adatbázisban minden UTC. |
| `DEBOUNCE_SECONDS` | nem | `60` | Ennyi időn belüli ismételt olvasás nem nyit és nem zár munkamenetet. |
| `AUTO_CLOSE_HOUR` | nem | `23:59` | Ekkor zárja le a nyitva maradt munkameneteket. `HH:MM` vagy csak óra. |
| `TEMP_MIN_ALERT` | nem | `16` | Hőmérséklet alsó riasztási küszöb (°C). |
| `TEMP_MAX_ALERT` | nem | `28` | Hőmérséklet felső riasztási küszöb (°C). |
| `HUM_MIN_ALERT` | nem | `25` | Páratartalom alsó riasztási küszöb (%). |
| `HUM_MAX_ALERT` | nem | `65` | Páratartalom felső riasztási küszöb (%). |
| `ENV_ALERTS_ENABLED` | nem | `true` | `false` esetén nincs környezeti figyelmeztetés a felületen. |
| `SALARY_ENABLED` | nem | `true` | `false` esetén a bérrel kapcsolatos oszlopok, nézetek és exportok nem jelennek meg, a hozzájuk tartozó útvonalak 404-et adnak. |
| `HOURLY_RATE` | nem | `1900` | **Alapértelmezett** órabér, Ft/óra. Csak arra a dolgozóra vonatkozik, akinek nincs saját órabér-sora. |
| `COOKIE_SECURE` | nem | `true` | Csak lokális http-s fejlesztéshez állítsd `false`-ra. |
| `SESSION_SECRET` | nem | származtatott | A session cookie aláírókulcsa. Ha nincs megadva, az `ADMIN_PASSWORD_HASH`-ből származik, így egy deploy nem lépteti ki az admint. |
| `GATEWAY_OFFLINE_MINUTES` | nem | `5` | Ennyi idő után számít offline-nak egy gateway a dashboardon. |

**A riasztás soha nem küld e-mailt, SMS-t vagy push értesítést** — csak a
felületen jelenik meg.

### A kulcsok előállítása

Gateway API kulcs:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Ugyanezt az értéket kell beírni a Railway `GATEWAY_API_KEY` változójába **és**
a gateway `.env` fájljába — a kettőnek egyeznie kell, különben a gateway 401-et
kap és a puffere korlátlanul nőni fog.

Admin jelszó hash:

```bash
cd server
python scripts/hash_password.py
# vagy nem interaktívan:
python scripts/hash_password.py "a-jelszavad"
```

A kiírt hash-t másold az `ADMIN_PASSWORD_HASH` változóba. A jelszót magát sehol
ne tárold.

## API — a gateway szerződése

Minden `/api/v1/*` végpont `X-API-Key: <GATEWAY_API_KEY>` fejlécet vár, kivéve a
`/api/v1/health`-et. Hiányzó vagy hibás kulcs → `401`.

### `POST /api/v1/events` — bélyegzések

```json
{
  "gateway_id": "iroda-fszt",
  "events": [
    {
      "event_uuid": "3f2a1c48-9d1e-4a3b-8c77-2e5f0b6a1d90",
      "uid": "04A1B2C3",
      "ts_local": "2026-09-06T08:31:12+02:00"
    }
  ]
}
```

Legfeljebb **500** esemény kötegenként, efölött `413`. Válasz `200`:

```json
{"accepted": ["uuid1"], "duplicates": ["uuid3"], "errors": [{"event_uuid": "uuid4", "reason": "invalid_timestamp"}]}
```

**Az `accepted` és a `duplicates` egyaránt sikeres feldolgozást jelent** — a
gateway mindkét listát törölheti a pufferéből. Egyetlen hibás esemény nem
buktatja el a köteget: a jók feldolgozódnak, a rossz az `errors` közé kerül
(`invalid_payload`, `invalid_timestamp`).

Ha a gateway újraküld egy már feldolgozott eseményt (mert nem kapta meg a
választ), a szerver csendben elfogadja és a `duplicates` közé teszi — nem hoz
létre második rekordot és nem ad hibát.

### `POST /api/v1/env` — környezeti mérések

Óránként egy sor: az adott óra átlaga, minimuma és maximuma. Legfeljebb **200**
mérés kötegenként, a válasz alakja ugyanaz.

```json
{
  "gateway_id": "iroda-fszt",
  "readings": [
    {
      "reading_uuid": "8b1c9f2e-0a44-4c31-9f7d-1b2c3d4e5f60",
      "sensor_id": "default",
      "period_start": "2026-09-06T08:00:00+02:00",
      "period_end":   "2026-09-06T09:00:00+02:00",
      "sample_count": 58,
      "temp_avg": 23.41, "temp_min": 22.90, "temp_max": 24.10,
      "hum_avg":  41.20, "hum_min":  39.80, "hum_max":  43.00,
      "press_avg": 1013.25
    }
  ]
}
```

Validáció a BME280 fizikai tartományai szerint: `-40 ≤ temp ≤ 85`,
`0 ≤ hum ≤ 100`, `300 ≤ press ≤ 1100`, `sample_count ≥ 1`. Tartományon kívüli
érték → `errors`, `reason: "out_of_range"`.

### `POST /api/v1/heartbeat`

```json
{"gateway_id": "iroda-fszt", "version": "1.0.0", "queue_size": 0, "serial_ok": true, "bme_ok": true}
```

Válasz: `{"ok": true, "server_time": "..."}`.

### `GET /api/v1/health`

Nem igényel API kulcsot. `{"status":"ok","db":"ok","version":"1.0.0"}`, illetve
`503`, ha az adatbázis nem elérhető.

### `GET /api/v1/env/series?hours=24`

A grafikon adatforrása. Session cookie-val hívható (a böngésző hívja), de API
kulcsot is elfogad, hogy parancssorból tesztelhető legyen.

Az interaktív API dokumentáció: `/api/docs`.

## Üzleti logika

**UID feloldás.** Ismeretlen kártya esetén a bélyegzés akkor is mentődik,
`employee_id=NULL`, `direction=NULL`, és megjelenik az adminban az „Ismeretlen
kártyák” listában. **Soha nem dobunk el adatot.** Hozzárendeléskor az összes
korábbi, még senkihez nem tartozó bélyegzés visszamenőleg megkapja a dolgozót,
és lefut rájuk az irányszámítás.

**IN/OUT irány.** A dolgozó nyitott munkamenete alapján váltakozik: nincs nyitott
→ `IN`, van nyitott → `OUT`, és lezárja a munkamenetet.

**Késve érkező események.** A feldolgozás **mindig `ts_utc` szerint rendezve**
történik, soha nem `received_at` szerint. Ha a gateway 3 óra offline után tölt
fel egy köteget, a `recalculate_directions(employee_id, from_ts)` újrajátssza az
érintett szakaszt és újraépíti a `work_sessions` sorokat. A `punches` tábla az
egyetlen igazságforrás, a munkamenetek belőle bármikor újraszámíthatók.

**Debounce.** Ugyanaz a `card_uid` a `DEBOUNCE_SECONDS`-on belül újra:
a bélyegzés mentődik `debounced` jelöléssel, de nem nyit és nem zár
munkamenetet, az iránya `NULL` marad.

**Automatikus zárás.** Az `AUTO_CLOSE_HOUR` időpontban egy háttérfeladat lezárja
a nyitva maradt munkameneteket `auto_closed=1` jelöléssel. Ezek a riportban
sárga háttérrel jelennek meg, mert kézi ellenőrzést igényelnek.

A zárási szabály az újraszámolásnak is része: egy nyitva felejtett munkamenet
akkor sem olvad össze a másnapi belépéssel, ha a bélyegzések utólag, egyszerre
érkeznek fel. Ha viszont később megjön a valódi kilépés, az felülírja az
automatikus zárást.

**Időzóna.** Az adatbázisban minden időbélyeg UTC. A `punches.ts_local` a
gateway által küldött nyers ISO stringet őrzi, auditáláshoz. A megjelenítés és a
naptári határok (nap, hónap) `zoneinfo.ZoneInfo("Europe/Budapest")` szerint
képződnek, `pytz` nélkül. Az óraátállítás napján a ledolgozott órák a valóban
eltelt időt mutatják, nem a faliórán látszó különbséget.

## Admin felület

| Útvonal | Tartalom |
|---|---|
| `/` | Ki van bent most, gateway státusz, mai bélyegzések, aktuális hőmérséklet és páratartalom, 24 órás grafikon |
| `/employees` | Dolgozók: hozzáadás, szerkesztés, inaktiválás |
| `/employees/{id}/rates` | Órabér-előzmény: új sor felvétele, hibás törlése |
| `/cards` | Kártyák és az ismeretlen UID-ok hozzárendelése |
| `/reports` | Napi bontás és havi összesítés, szűrés dolgozóra és dátumtartományra; alul a bérszámítás |
| `/reports/export` | Jelenléti CSV export (UTF-8 BOM, pontosvessző elválasztó) |
| `/reports/salary/export` | Bér CSV export: dolgozóval napi bontás, nélküle havi összesítő |
| `/environment` | Környezeti adatok 24 óra / 7 nap / 30 nap bontásban, grafikon, táblázat, CSV |
| `/punches` | Nyers eseménynapló, szűrés, kézi javítás, audit napló |

Belépés: egyetlen admin jelszóval, aláírt session cookie-val
(`httponly`, `secure`, `samesite=lax`).

**Kézi javítás:** egy bélyegzés iránya felülírható, a bélyegzés törölhető, és új
vehető fel. Minden ilyen művelet bekerül az `audit_log` táblába a régi és az új
értékkel együtt, és utána lefut a `recalculate_directions`. A kézzel megadott
irány horgonyként viselkedik: a későbbi újraszámolás nem írja felül.

## Bérszámítás

Tájékoztató bruttó összeg a ledolgozott órák alapján. **Nem bérszámfejtés:** nincs
benne adó, járulék, pótlék, szabadság vagy táppénz.

### Órabér dolgozónként, előzménnyel

Az órabér nem egy mező a dolgozón, hanem az `employee_rates` tábla sorai, mindegyik
egy érvényességi kezdődátummal. Egy adott naphoz az a sor tartozik, aminek a
`valid_from` értéke a legnagyobb az adott dátumnál nem későbbiek közül.

Ezért nem írja át visszamenőleg a régi hónapokat, ha valaki emelést kap: a februári
kimutatás a februárban érvényes órabérrel számol akkor is, ha márciustól új sor lép
életbe. Meglévő sort nem lehet szerkeszteni — csak újat felvenni vagy hibásat törölni.

**Egy sor törlése viszont visszamenőleg átírja a korábbi kimutatásokat** — pontosan
az, ami ellen ez a tábla véd. Ezért a törlés külön megerősítő lapot kér, ami megmutatja,
mely hónapok összege mennyivel változna (`2026-09: 16 000 Ft → 8 000 Ft, −8 000 Ft`),
és mi lép a törölt sor helyébe. Megerősítés nélküli POST nem töröl, csak visszairányít
erre a lapra.

Minden bérsor-művelet bekerül az `audit_log` táblába a régi **és** az új értékkel:
felvételnél az, hogy mit vált fel az új sor, törlésnél az, hogy mi lép a helyébe és
mely hónapokat érinti.

Ha egy dolgozónak egyáltalán nincs sora, a `HOURLY_RATE` env érték az alapértelmezés.
A felület ezt „alapértelmezett" jelöléssel mutatja, hogy látszódjon, kinél nincs még
beállítva a saját órabér.

Kezelés: **Dolgozók → Órabér** (`/employees/{id}/rates`).

### Kerekítés

A másodperceket összegezzük, és **csak a napi sor végén kerekítünk** egész forintra,
`Decimal`-lal, félnél felfelé. A havi összeg a napi összegek szummája, nem a hónap
nyers másodperceiből újraszámolt érték — így a felületen látható napi sorok pontosan
kiadják a havi végösszeget.

A művelet sorrendje is számít: **előbb szorzunk, aztán osztunk**
(`másodperc × órabér / 3600`). A másodperc/óra hányados szakaszos tizedestört
(3600 = 2⁴ · 3² · 5²), a `Decimal` pontossága pedig véges — fordított sorrendben egy
levágott hányadost szoroznánk fel. Így a szorzat egzakt egész marad, és egyetlen
osztás van a végén. Teszt hasonlítja össze az eredményt egzakt racionális
aritmetikával (`fractions.Fraction`).

Külön teszt őrzi, hogy a CSV exportban szereplő összeg **soronként és a végösszegben
is pontosan egyezik** a képernyőn megjelenővel — ez fogná meg, ha valaha visszakerülne
a kétszeres kerekítés.

```
munkamenetek (csak lezártak)
   → napi csoport a kezdés lokális napja szerint
   → sum(duration_seconds)
   → × az adott NAPRA érvényes órabér
   → kerekítés egész Ft-ra, EGYSZER
   → havi összeg = Σ napi összeg
```

Az éjfélen átnyúló műszak ahhoz a naphoz tartozik, amelyiken elkezdődött. A még le
nem zárt munkamenetek nem számítanak bele semmibe — a jelenléti riport mutatja az
eddig eltelt időt, a bér nem számol vele.

Ez viszont nem csendben történik: ha az adott hónapban van folyamatban lévő
munkamenet, a bér-nézetben megjelenik egy halvány sor — *„1 folyamatban lévő
munkamenet, a bérbe nem számítva"* —, az érintett napi sor pedig „folyamatban"
jelölést kap. Enélkül az irodavezető délután megnyitná a bérkimutatást, kevesebb órát
látna, mint a jelenlétiben, és azt hinné, hibás a rendszer. Aki csak folyamatban lévő
munkamenettel rendelkezik, az is megjelenik az összesítőben, nulla forinttal.

### Ellenőrzést igénylő tételek

Az `auto_closed` munkamenetek (amiket a rendszer az `AUTO_CLOSE_HOUR` időpontjában zárt le, mert
valaki elfelejtett kijelentkezni) **beleszámítanak** az összegbe — kihagyva hiányozna
a pénz. De sárga háttérrel és ⚠ ikonnal jelennek meg, és külön is összesítve:
„Ebből ellenőrzést igényel: X nap, Y óra, Z Ft". Ez az az összeg, amit kifizetés
előtt valakinek kézzel jóvá kell hagynia. A részösszeg csak az automatikusan lezárt
munkameneteket tartalmazza, nem a teljes napot.

### Nézetek

A `/reports` oldal alján, két blokkban:

* **Havi nézet** — egy dolgozó egy hónapja, naponként egy sorral: dátum, be-ki
  időpont, ledolgozott óra, órabér, összeg
* **Összesítő** — egy hónap minden dolgozóval: összes óra, átlagos órabér, összeg.
  Ez az a nézet, amit a könyvelőnek lehet odaadni.

CSV export mindkettőhöz: `/reports/salary/export?month=YYYY-MM[&employee_id=N]`,
`utf-8-sig` kódolással és pontosvessző elválasztóval, külön oszlopban az
`auto_closed` jelöléssel.

A jelenléti CSV (`/reports/export`) szándékosan **nem tartalmaz pénzt**: ott a sorok
munkamenetenkéntiek, és munkamenetenként szorozni óradíjat kerekítési hibát vinne
bele. A pénz a napi összesítésből számol, azt a bér-export adja.

## Adatmegőrzés

A `punches` és a `work_sessions` sorokat **soha nem törli automatikusan semmi** —
ez munkaügyi adat. A dolgozók és a kártyák nem törölhetők, csak inaktiválhatók.
Az `env_readings` óránként egy sorral nő (évi ~8760 sor), erre sincs automatikus
törlés.

## Fejlesztés

```bash
cd server
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # töltsd ki: GATEWAY_API_KEY, ADMIN_PASSWORD_HASH
python scripts/hash_password.py

DB_PATH=./dev.db COOKIE_SECURE=false uvicorn app.main:app --reload --port 8000
```

Tesztek:

```bash
cd server
python -m pytest
```

A tesztek lefedik az idempotenciát, az IN/OUT sorrendet, a késve érkező
események újraszámolását, a debounce-ot, az ismeretlen UID visszamenőleges
feldolgozását, a nyári/téli időszámítás váltását, a hibatűrő batch-feldolgozást
és a tartomány-validációt.
