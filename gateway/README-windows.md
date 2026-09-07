# Iroda RFID gateway – Windows 10 telepítés és üzemeltetés

Ez a komponens a lánc közepe:

```
Arduino Nano (RC522 RFID olvasó + BME280 szenzor + buzzer)
    │ USB soros, 115200 baud
    ▼
Windows 10 mini PC (Gigabyte GB-BXBT-1900)  ←  EZ A MAPPA
    │ HTTPS POST
    ▼
Railway: https://iroda-szerver.up.railway.app
```

A gateway olvas, időbélyeget tesz rá, lemezre menti, feltölti. **Nem dönti el,
hogy belépés vagy kilépés, nem tudja, kié a kártya, nem számol munkaidőt és nem
értékeli a hőmérsékletet** – ez mind a szerveren fut. Egyetlen kivétel a
környezeti mérések órás összesítése, hogy a napközbeni csúcsértékek is
megmaradjanak.

Ha a hálózat elmegy, a bélyegzések a lemezen gyűlnek, és a kapcsolat
visszatértekor mennek fel. Ha közben áramszünet van, a már beolvasott
bélyegzések akkor is megvannak: a mentés a feltöltés előtt történik.

---

## 1. Telepítés lépésről lépésre

### 1.1 Python 3.11 vagy újabb

1. Nyisd meg: <https://www.python.org/downloads/windows/>
2. Töltsd le a **Windows installer (64-bit)** változatot.
3. A telepítő **első képernyőjén**:
   - pipáld be: **Add python.exe to PATH**
   - kattints: **Customize installation**
4. Az „Optional Features" oldalon hagyj mindent bepipálva → **Next**
5. Az „Advanced Options" oldalon pipáld be: **Install for all users**
   *(Ez fontos: a szolgáltatás nem a te felhasználói fiókoddal fut. Ha csak
   magadnak telepíted, a szolgáltatás nem találja meg a Pythont.)*
6. **Install** → a végén **Close**
7. Ellenőrzés: `Win+R` → `cmd` → Enter, majd:
   ```
   python --version
   ```
   `Python 3.11.x` (vagy újabb) kell hogy megjelenjen.

### 1.2 A repó letöltése

Ha van git a gépen:

```
cd C:\
git clone https://github.com/babakristof888-lab/iroda-system.git
```

Ha nincs git: a GitHub oldalon **Code → Download ZIP**, majd csomagold ki ide:
`C:\iroda-system`.

### 1.3 A `.env` fájl kitöltése

```
cd C:\iroda-system\gateway
copy .env.example .env
notepad .env
```

Amit ki kell tölteni:

| Sor | Mit írj bele |
|---|---|
| `GATEWAY_API_KEY=` | **Pontosan** ugyanaz, mint a Railway-en a `GATEWAY_API_KEY` változó értéke. Egyetlen karakter eltérés is 401-et okoz. |
| `GATEWAY_ID=` | A gateway neve az admin felületen, pl. `iroda-fszt` |
| `SERIAL_PORT=` | Hagyd üresen az első telepítéskor: automatikusan megkeresi. Ha a 2. fejezet szerint fix COM-portot adtál az eszköznek, írd ide (pl. `COM5`). |

Mentés, bezárás. **A `.env` fájl sosem kerül a gitbe**, a kulcs nem szivárog ki.

### 1.4 A szolgáltatás telepítése

1. Start menü → írd be: `PowerShell`
2. **Jobb gomb** a „Windows PowerShell" bejegyzésen → **Futtatás rendszergazdaként**
3. A megnyíló ablakban:

```
cd C:\iroda-system\gateway
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\install_windows.ps1
```

A script ellenőrzi a Pythont, létrehozza a `.venv`-et, telepíti a három
függőséget, létrehozza a `C:\ProgramData\IrodaGateway\logs` mappát, letölti az
NSSM-et, és regisztrálja a `RfidGateway` szolgáltatást automatikus indítással.

### 1.5 Ellenőrzés

```
.venv\Scripts\python.exe check.py
```

Ez kiírja, melyik COM portot találta, mit válaszol az Arduino, hány elem vár a
pufferben, és elérhető-e a szerver. Mivel a szolgáltatás közben fogja a portot,
a portos rész „foglalt" választ ad – ez ilyenkor **normális**. Teljes
vizsgálathoz előbb `nssm stop RfidGateway`, utána `nssm start RfidGateway`.

Húzz le egy kártyát az olvasón, majd:

```
type C:\ProgramData\IrodaGateway\logs\gateway.log
```

Látnod kell egy `Kártya olvasva: UID=...` sort, és nem sokkal utána a
feltöltést. Az admin felület dashboardján is meg kell jelennie a gateway-nek
„online" állapotban.

---

## 2. Windows beállítások ellenőrzőlistája

**Ezek nélkül a rendszer néhány hét múlva elkezd furcsán viselkedni.**
Tipikusan úgy, hogy éjszaka „eltűnik" a COM port, és reggelig nem bélyegez
senki. Menj végig mindegyiken.

### 2.1 USB selective suspend kikapcsolása

`Vezérlőpult → Hardver és hang → Energiagazdálkodási lehetőségek →
A séma beállításainak módosítása → Speciális energiaellátási beállítások
módosítása → USB-beállítások → USB szelektív felfüggesztés beállítása →
**Letiltva**` (mind „Akkumulátoros", mind „Hálózati" alatt, ha látszik).

### 2.2 Az eszköz energiagazdálkodása az Eszközkezelőben

`Win+X → Eszközkezelő`

Két helyen kell kivenni ugyanazt a pipát:

1. **Universal Serial Bus vezérlők** → minden **USB Root Hub** elem →
   jobb gomb → Tulajdonságok → **Energiagazdálkodás** fül →
   *„A számítógép kikapcsolhatja az eszközt az energiatakarékosság érdekében"* →
   **pipa ki**
2. **Portok (COM és LPT)** → a soros eszköz (pl. „USB-SERIAL CH340 (COM5)") →
   jobb gomb → Tulajdonságok → **Energiagazdálkodás** fül → ugyanaz a pipa ki
   *(ha van ilyen fül ennél az eszköznél)*

### 2.3 Hibernálás és Fast Startup kikapcsolása

Rendszergazdai parancssorban:

```
powercfg /h off
```

A Fast Startup (gyors indítás) ugyanezzel megszűnik. Erre azért van szükség,
mert a gyors indítás nem valódi újraindulás: az USB eszközök állapota
kiszámíthatatlan marad utána.

### 2.4 Alvás: soha

`Beállítások → Rendszer → Bekapcsolás és alvás`:

- Képernyő: mindegy
- **Alvás: Soha**

### 2.5 Fix COM port szám

Ha az eszköz COM-száma minden újradugásnál változik, rögzítsd:

`Eszközkezelő → Portok (COM és LPT) → az eszköz → jobb gomb → Tulajdonságok →
**Port beállításai** fül → **Speciális** → **COM-port száma**` → válassz egy
számot (pl. COM5) → OK.

Utána írd be a `.env` fájlba is: `SERIAL_PORT=COM5`, és `nssm restart RfidGateway`.

### 2.6 Windows Defender kizárás

`Beállítások → Adatvédelem és biztonság → Windows biztonság →
Vírus- és veszélyforrás-kezelés → Beállítások kezelése →
Kizárások → Kizárások hozzáadása vagy eltávolítása → Mappa hozzáadása`:

```
C:\ProgramData\IrodaGateway
```

Enélkül a valós idejű vizsgálat időnként ráül az SQLite fájlra, és lassú vagy
akadozó írásokat okoz.

### 2.7 BIOS: áramszünet után induljon el magától

Újraindítás → `DEL` (Gigabyte alaplapon) a BIOS-ba:

`Chipset` vagy `Power Management` → **Restore on AC Power Loss** → **Power On**

Enélkül egy áramszünet után a gép kikapcsolva marad, és senki nem tud
bélyegezni, amíg valaki oda nem megy megnyomni a gombot.

---

## 3. Hibakeresés

### 3.1 Hol vannak a naplók

```
C:\ProgramData\IrodaGateway\logs\gateway.log   <- ez a fontos (5 MB × 3 fájl)
C:\ProgramData\IrodaGateway\logs\out.log       <- amit az NSSM fog el a stdoutról
C:\ProgramData\IrodaGateway\logs\err.log       <- indulási hibák
```

Az utolsó 50 sor megnézése:

```
Get-Content C:\ProgramData\IrodaGateway\logs\gateway.log -Tail 50
```

Élő követés (mint a `tail -f`):

```
Get-Content C:\ProgramData\IrodaGateway\logs\gateway.log -Wait -Tail 20
```

### 3.2 A szolgáltatás kezelése

```
nssm status  RfidGateway
nssm restart RfidGateway
nssm stop    RfidGateway
nssm start   RfidGateway
nssm edit    RfidGateway     # grafikus beállítóablak
```

Ha az `nssm` parancsot nem találja, a teljes útvonallal hívd:
`C:\iroda-system\gateway\nssm\nssm-2.24\win64\nssm.exe restart RfidGateway`

**A `.env` módosítása után mindig kell egy `nssm restart RfidGateway`** – a
beállítások induláskor olvasódnak be.

### 3.3 „A COM5 port foglalt"

A naplóban ez a sor:

```
A COM5 port foglalt. Zárd be az Arduino IDE Serial Monitorát, vagy más programot, ami használja.
```

Windows-on egy soros portot egyszerre csak egy program nyithat meg. A gateway
5 másodpercenként újrapróbálja, tehát ha bezárod a másik programot, magától
helyreáll – nem kell újraindítani semmit.

Leggyakoribb okok, sorrendben:

1. **Nyitva van az Arduino IDE Serial Monitor** ablaka. Zárd be. (Maga az
   Arduino IDE nyitva maradhat, csak a Serial Monitor ne.)
2. **A `check.py` fut** egy másik ablakban. Zárd be.
3. **Két példányban fut a szolgáltatás.** `nssm status RfidGateway`, és nézd
   meg a Feladatkezelőben, hány `python.exe` fut.
4. Valamilyen más soros terminál (PuTTY, CoolTerm) tartja nyitva.

### 3.4 Ha a `queue_size` folyamatosan nő

A `queue_size` az admin dashboardon a még fel nem töltött elemek száma.
Ha ez nem áll vissza nullára, a bélyegzések a lemezen gyűlnek – **nem vesznek
el**, csak nem látszanak a felületen. Ilyen sorrendben nézd:

1. **Rossz az API kulcs.** A naplóban:
   `A szerver elutasította a kulcsot (HTTP 401)`.
   Ellenőrizd, hogy a `.env` `GATEWAY_API_KEY` értéke **karakterre pontosan**
   ugyanaz-e, mint a Railway service Variables fülén. Utána
   `nssm restart RfidGateway`. A puffer magától felmegy.
2. **Nincs internet.** A naplóban `Hálózati hiba` sorok. Próbáld:
   `curl https://iroda-szerver.up.railway.app/api/v1/health`
   vagy futtasd a `check.py`-t.
3. **A szerver van lent.** A naplóban `HTTP 502` / `HTTP 503`. Nézd meg a
   Railway felületén a service állapotát. A gateway magától újrapróbálkozik,
   egyre ritkábban (5s → 10s → 20s → … → max 5 perc), és amint a szerver
   visszajön, mindent felküld.
4. **A szerver visszautasított sorokat.** A naplóban
   `a szerver visszautasította (invalid_timestamp)`. Ezek `error` státuszba
   kerülnek, nem próbáljuk őket újra (sosem sikerülnének), és soha nem is
   töröljük őket. A `check.py` kiírja, hány ilyen van. Ha ilyet látsz, szólj –
   ez általában elállított rendszeróra jele.

**A számok megnézése bármikor:** `.venv\Scripts\python.exe check.py`

### 3.5 `ERR;BME_OFFLINE` a naplóban

A BME280 hőmérséklet-/páratartalom-szenzor nem válaszol az Arduinónak.
Következménye: az admin felület `bme_ok=false`-t mutat, és arra az órára nem
készül környezeti sor (üres sort szándékosan nem küldünk – a hiány így látszik
a grafikonon).

A bélyegzés ettől **tökéletesen működik tovább**, ez nem sürgős hiba.

Teendő, ha zavar:

1. Nézd meg a szenzor négy vezetékét az Arduinón (VCC, GND, SDA=A4, SCL=A5).
   A leggyakoribb ok egy kilazult dugaszolós vezeték.
2. Ha visszajön, a naplóban megjelenik: `A BME280 szenzor visszajött
   (OK;BME_RECOVERED)` – nem kell újraindítani semmit.
3. Ha a `check.py` a `RDY` sorban `bme=0x00`-t ír, a szenzort az Arduino már
   induláskor sem látta.

Ha egyáltalán nincs szenzor a rendszerben, tedd a `.env`-be:
`ENV_ENABLED=false` – így a gateway nem is gyűjt környezeti adatot.

### 3.6 `ERR;RC522_OFFLINE`

Az RFID olvasó nem válaszol. **Ez sürgős**: ilyenkor nem lehet bélyegezni.
Az admin dashboardon `serial_ok=false` látszik. Nézd meg az olvasó kábelezését
az Arduinón. Amint helyreáll, `OK;RC522_RECOVERED` jelenik meg a naplóban.

### 3.7 A gateway „offline" az admin felületen

A gateway percenként küld heartbeatet; a szerver 5 perc után jelöli offline-nak.

1. Fut a szolgáltatás? `nssm status RfidGateway`
2. Ha nem: `C:\ProgramData\IrodaGateway\logs\err.log`
3. Ha fut, de offline: nincs internet, vagy rossz az API kulcs (lásd 3.4).

---

## 4. Frissítés

```
cd C:\iroda-system
git pull
nssm restart RfidGateway
```

Ha a `requirements.txt` is változott:

```
cd C:\iroda-system\gateway
.venv\Scripts\python.exe -m pip install -r requirements.txt
nssm restart RfidGateway
```

A `.env` fájlt és a `C:\ProgramData\IrodaGateway` alatti adatokat a frissítés
nem érinti. Az újraindítás nem veszít adatot: ami a pufferben van, az a
következő körben megy fel.

## 5. Eltávolítás

PowerShell rendszergazdaként:

```
cd C:\iroda-system\gateway
.\uninstall_windows.ps1
```

A szolgáltatást leállítja és eltávolítja. **Az adatokat nem törli**, csak
rákérdez – a `C:\ProgramData\IrodaGateway` alatt még lehetnek fel nem töltött
bélyegzések.

---

## 6. Ami hol van

| Útvonal | Mi ez |
|---|---|
| `C:\iroda-system\gateway\` | a program és a `.env` |
| `C:\iroda-system\gateway\.venv\` | a Python virtuális környezet |
| `C:\ProgramData\IrodaGateway\queue.db` | a puffer (SQLite) |
| `C:\ProgramData\IrodaGateway\logs\` | naplók |

A `queue.db` a `punch_queue` (bélyegzések), az `env_samples` (percenkénti nyers
mérések) és az `env_queue` (órás összesítők) táblákat tartalmazza. A már
feltöltött sorokat egy napi karbantartás 30 nap után törli, a feldolgozott nyers
mintákat 7 nap után. **A `error` státuszú sorokat soha nem törli semmi.**

## 7. Fejlesztés / tesztek

A tesztekhez nem kell se Arduino, se hálózat:

```
cd C:\iroda-system\gateway
.venv\Scripts\python.exe -m pytest
```
