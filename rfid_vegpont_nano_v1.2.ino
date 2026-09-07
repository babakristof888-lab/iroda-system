/*
 * RFID munkaido-nyilvantarto rendszer - VEGPONT
 * v1.2.0
 * ---------------------------------------------
 * Hardver : Arduino Nano (ATmega328P)
 *           + MFRC522 (RC522) RFID olvaso, SPI buszon
 *           + BME280 homerseklet/paratartalom/legnyomas szenzor, I2C buszon
 *           + passziv buzzer es ket LED visszajelzeshez
 *
 * VALTOZASOK a v1.1.0 ota
 *   - LED_ACTIVE_LOW kapcsolo: kezeli a forditva (kozos anodos) bekotott LED-eket
 *   - A zold LED PWM-mel tompithato, hogy ejjel ne vilagitsa be a folyosot
 *   - Uj soros parancsok: LED <0-255> es LEDTEST
 *
 * ============================ BEKOTES ============================
 *
 *  MFRC522 (SPI)              BME280 (I2C)           Visszajelzes
 *  --------------------       ------------------     ---------------------
 *  Nano 3V3 -> 3.3V           Nano 3V3 -> VIN        Nano D6 -> buzzer (+)
 *  Nano D9  -> RST            Nano GND -> GND        Nano D5 -> zold LED
 *  Nano GND -> GND            Nano A4  -> SDA        Nano D4 -> piros LED
 *  Nano D12 -> MISO           Nano A5  -> SCL
 *  Nano D11 -> MOSI
 *  Nano D13 -> SCK            A buzzer masik laba GND.
 *  Nano D10 -> SDA (SS)       A LED-ek 220 ohm ellenallason at.
 *  (IRQ bekotetlen)
 *
 *  FIGYELEM 1: az RC522 kizarolag 3,3 V-ot kap. 5 V-ra kotve tonkremegy.
 *
 *  FIGYELEM 2: a Nano 3,3 V-os laba az USB-soros chiprol jon, ez kb. 50 mA.
 *  Ket modulhoz tegy be egy AMS1117-3.3 modult az 5 V-rol taplalva.
 *
 *  FIGYELEM 3: a BME280 modult tedd rovid kabelen a dobozon kivulre.
 *
 * ============================ KONYVTARAK ============================
 *   MFRC522           by GithubCommunity (miguelbalboa), 1.4.x
 *   Adafruit BME280   by Adafruit
 *   Adafruit Unified Sensor  by Adafruit
 *
 * ============================ SOROS PROTOKOLL ============================
 *   115200 baud, sorvegi \n
 *
 *   Kimeno:
 *     RDY;fw=1.2.0;rc522=0x92;bme=0x76   induláskor
 *     CARD;04A1B2C3                      kartya olvasva
 *     DUP;04A1B2C3                       kartya a debounce ablakon belul
 *     ENV;T=23.45;H=41.20;P=1013.25      kornyezeti meres (C, %, hPa)
 *     PONG;fw=1.2.0                      PING parancsra
 *     LEDOK;<0-255>                      a LED parancs nyugtazasa
 *     ERR;RC522_OFFLINE                  az RFID olvaso nem valaszol
 *     OK;RC522_RECOVERED                 sikeres ujrainicializalas
 *     ERR;BME_OFFLINE                    a szenzor nem valaszol
 *     OK;BME_RECOVERED                   a szenzor visszajott
 *
 *   Bejovo:
 *     PING          eletjel-kerdes
 *     VER           verzio + modul-allapot
 *     ENV           azonnali kornyezeti meres
 *     LED <0-255>   a zold LED fenyereje (0 = teljesen sotet)
 *                   Igy a gateway este lehalkithatja, reggel visszakapcsolhatja.
 *     LEDTEST       mindket LED felvillantasa a bekotes ellenorzesehez
 */

#include <SPI.h>
#include <Wire.h>
#include <MFRC522.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_BME280.h>

// ---------- Konfiguracio ----------
#define FW_VERSION        "1.2.0"

#define RST_PIN           9
#define SS_PIN            10

#define USE_FEEDBACK      1
#define BUZZER_PIN        6
#define LED_OK_PIN        5        // PWM-kepes, ezert tomapithato
#define LED_ERR_PIN       4        // NEM PWM-kepes, csak be/ki

/* ======================================================================
 * LED POLARITAS - EZ AZ, AMI NALAD MOST FORDITVA VOLT
 * ----------------------------------------------------------------------
 * 1 = KOZOS ANODOS (aktiv-alacsony) bekotes:
 *       a LED hosszu laba (anod) a TAPRA megy,
 *       a rovid laba az ellenallason at az Arduino pinjere.
 *       Ilyenkor a pin LOW allapota gyujtja meg a LED-et.
 *       TUNET, HA EZ ROSSZUL VAN ALLITVA:
 *       a LED-ek folyamatosan vilagitanak, es olvasaskor egy pillanatra
 *       ELALSZANAK ahelyett, hogy felvillannanak.
 *
 * 0 = HAGYOMANYOS (aktiv-magas) bekotes:
 *       a LED hosszu laba (anod) az Arduino pinjere megy,
 *       a rovid laba az ellenallason at a GND-re.
 *       Ilyenkor a pin HIGH allapota gyujtja meg a LED-et.
 *
 * Ha atforrasztod a LED-eket a hagyomanyos modon, allitsd 0-ra.
 * Addig hagyd 1-en, es minden helyesen fog mukodni forrasztas nelkul.
 * Ellenorzes: a LEDTEST parancs felvillantja oket egyesevel.
 * ====================================================================== */
#define LED_ACTIVE_LOW    1

// A zold LED alap fenyereje (0-255). A 40 nappal is jol lathato,
// ejjel viszont nem vilagitja be a folyosot. 255 = teljes fenyero.
#define LED_OK_BRIGHTNESS 40

// Ugyanaz a kartya ennyi ideig nem szamit uj olvasasnak.
#define DEBOUNCE_MS       3000UL

// Milyen surun kuldjon kornyezeti merest. NEM orankent: a gateway gyujti
// ezeket, es o kuldi fel orankent az atlagot, minimumot es maximumot.
#define ENV_INTERVAL_MS   60000UL

// Ilyen surun ellenorzi, hogy az RFID olvaso valaszol-e.
#define HEALTHCHECK_MS    5000UL

// Regi bootloaderes Nano kloonokon a watchdog-reset vegtelen ujraindulasi
// ciklust okoz. Csak akkor kapcsold be, ha biztosan Optiboot van a lapkan.
#define USE_WATCHDOG      0

#if USE_WATCHDOG
  #include <avr/wdt.h>
#endif

// ---------- Allapot ----------
MFRC522 mfrc522(SS_PIN, RST_PIN);
Adafruit_BME280 bme;

byte     bmeAddr       = 0;
bool     bmeOnline     = false;
uint32_t lastEnvMs     = 0;

byte     lastUid[10];
byte     lastUidSize   = 0;
uint32_t lastReadMs    = 0;
uint32_t lastHealthMs  = 0;
bool     readerOnline  = false;

uint8_t  ledOkLevel    = LED_OK_BRIGHTNESS;   // futasidoben allithato
uint32_t ledOkOffAt    = 0;
uint32_t ledErrOffAt   = 0;

char     cmdBuf[20];
uint8_t  cmdLen        = 0;

// ---------------------------------------------------------------
// LED-vezerles - itt es CSAK itt dol el a polaritas.
// A kod tobbi resze mindig azt mondja, amit akar (be/ki), nem azt,
// hogy HIGH vagy LOW. Igy egyetlen #define atallitasa mindent megfordit.
// ---------------------------------------------------------------

void ledOkWrite(uint8_t brightness) {
#if LED_ACTIVE_LOW
  analogWrite(LED_OK_PIN, 255 - brightness);
#else
  analogWrite(LED_OK_PIN, brightness);
#endif
}

void ledErrWrite(bool on) {
#if LED_ACTIVE_LOW
  digitalWrite(LED_ERR_PIN, on ? LOW : HIGH);
#else
  digitalWrite(LED_ERR_PIN, on ? HIGH : LOW);
#endif
}

void ledsOff() {
  ledOkWrite(0);
  ledErrWrite(false);
}

// ---------- RFID segedfuggvenyek ----------

byte readVersion() {
  return mfrc522.PCD_ReadRegister(MFRC522::VersionReg);
}

bool initReader() {
  mfrc522.PCD_Init();
  delay(50);
  byte v = readVersion();
  if (v == 0x00 || v == 0xFF) return false;
  mfrc522.PCD_AntennaOn();
  return true;
}

void uidToHex(byte *uid, byte size, char *out) {
  const char hexChars[] = "0123456789ABCDEF";
  for (byte i = 0; i < size; i++) {
    out[i * 2]     = hexChars[(uid[i] >> 4) & 0x0F];
    out[i * 2 + 1] = hexChars[uid[i] & 0x0F];
  }
  out[size * 2] = '\0';
}

bool sameAsLast(byte *uid, byte size) {
  if (size != lastUidSize) return false;
  for (byte i = 0; i < size; i++) if (uid[i] != lastUid[i]) return false;
  return true;
}

void rememberUid(byte *uid, byte size) {
  lastUidSize = size;
  for (byte i = 0; i < size && i < 10; i++) lastUid[i] = uid[i];
  lastReadMs = millis();
}

// ---------- Visszajelzes (teljesen nem blokkolo) ----------

#if USE_FEEDBACK
void beepOk() {
  tone(BUZZER_PIN, 2200, 90);
  ledOkWrite(ledOkLevel);
  ledOkOffAt = millis() + 150;
}

void beepDup() {
  tone(BUZZER_PIN, 1100, 40);
  ledOkWrite(ledOkLevel);
  ledOkOffAt = millis() + 60;
}

void beepErr() {
  tone(BUZZER_PIN, 400, 300);
  ledErrWrite(true);
  ledErrOffAt = millis() + 400;
}

void serviceLeds(uint32_t now) {
  if (ledOkOffAt  && (int32_t)(now - ledOkOffAt)  >= 0) {
    ledOkWrite(0);  ledOkOffAt = 0;
  }
  if (ledErrOffAt && (int32_t)(now - ledErrOffAt) >= 0) {
    ledErrWrite(false); ledErrOffAt = 0;
  }
}

// Egyesevel felvillantja a LED-eket, hogy ellenorizni tudd a polaritast.
// Ha ez alatt a LED-ek ELALSZANAK ahelyett, hogy vilagitananak,
// akkor a LED_ACTIVE_LOW ertek rossz.
void ledTest() {
  ledsOff();
  delay(400);
  ledOkWrite(255);   delay(700);  ledOkWrite(0);
  delay(300);
  ledErrWrite(true); delay(700);  ledErrWrite(false);
}
#else
void beepOk()  {}
void beepDup() {}
void beepErr() {}
void serviceLeds(uint32_t now) { (void)now; }
void ledTest() {}
#endif

// ---------- BME280 ----------

bool initBme() {
  if      (bme.begin(0x76, &Wire)) bmeAddr = 0x76;
  else if (bme.begin(0x77, &Wire)) bmeAddr = 0x77;
  else { bmeAddr = 0; return false; }

  // Forced mod: meres utan a szenzor alszik. Folyamatos modban a chip
  // sajat melegedese 0,5-1 fokkal meghamisitana a merest.
  bme.setSampling(Adafruit_BME280::MODE_FORCED,
                  Adafruit_BME280::SAMPLING_X1,
                  Adafruit_BME280::SAMPLING_X1,
                  Adafruit_BME280::SAMPLING_X1,
                  Adafruit_BME280::FILTER_OFF);
  return true;
}

void sendEnv() {
  if (!bmeOnline) return;

  if (!bme.takeForcedMeasurement()) {
    bmeOnline = false;
    Serial.println(F("ERR;BME_OFFLINE"));
    return;
  }

  float t = bme.readTemperature();
  float h = bme.readHumidity();
  float p = bme.readPressure() / 100.0F;

  if (isnan(t) || isnan(h) || isnan(p)) {
    bmeOnline = false;
    Serial.println(F("ERR;BME_OFFLINE"));
    return;
  }

  char bufT[10], bufH[10], bufP[12];
  dtostrf(t, 0, 2, bufT);
  dtostrf(h, 0, 2, bufH);
  dtostrf(p, 0, 2, bufP);

  Serial.print(F("ENV;T=")); Serial.print(bufT);
  Serial.print(F(";H="));    Serial.print(bufH);
  Serial.print(F(";P="));    Serial.println(bufP);
}

// ---------- Soros parancsok ----------

void printStatus() {
  Serial.print(F("RDY;fw="));
  Serial.print(F(FW_VERSION));
  Serial.print(F(";rc522=0x"));
  Serial.print(readVersion(), HEX);
  Serial.print(F(";bme="));
  if (bmeAddr) { Serial.print(F("0x")); Serial.println(bmeAddr, HEX); }
  else         { Serial.println(F("none")); }
}

void handleSerialCommands() {
  while (Serial.available() > 0) {
    char c = Serial.read();

    if (c == '\n' || c == '\r') {
      if (cmdLen > 0) {
        cmdBuf[cmdLen] = '\0';

        if (strcmp(cmdBuf, "PING") == 0) {
          Serial.print(F("PONG;fw="));
          Serial.println(F(FW_VERSION));

        } else if (strcmp(cmdBuf, "VER") == 0) {
          printStatus();

        } else if (strcmp(cmdBuf, "ENV") == 0) {
          sendEnv();

        } else if (strcmp(cmdBuf, "LEDTEST") == 0) {
          ledTest();
          Serial.println(F("LEDOK;test"));

        } else if (strncmp(cmdBuf, "LED ", 4) == 0) {
          // "LED 0" .. "LED 255" - a zold LED fenyereje
          int v = atoi(cmdBuf + 4);
          if (v < 0)   v = 0;
          if (v > 255) v = 255;
          ledOkLevel = (uint8_t)v;
          Serial.print(F("LEDOK;"));
          Serial.println(ledOkLevel);
        }

        cmdLen = 0;
      }
    } else if (cmdLen < sizeof(cmdBuf) - 1) {
      cmdBuf[cmdLen++] = c;
    } else {
      cmdLen = 0;  // tul hosszu sor, eldobjuk
    }
  }
}

// ---------- Setup ----------
void setup() {
  Serial.begin(115200);

#if USE_FEEDBACK
  pinMode(LED_OK_PIN, OUTPUT);
  pinMode(LED_ERR_PIN, OUTPUT);
  pinMode(BUZZER_PIN, OUTPUT);
  ledsOff();     // FONTOS: azonnal sotetre, mielott barmi mas tortenne
#endif

  SPI.begin();

  Wire.begin();
  // A Wire konyvtar alapbol bekapcsolja az ATmega belso felhuzoit, amik
  // 5 V-ra huznak. Ha a BME280 modulon nincs szintillesztő, ez lassan
  // tonkreteszi a szenzort. Ez a ket sor kikapcsolja oket.
  digitalWrite(SDA, LOW);
  digitalWrite(SCL, LOW);

  bmeOnline    = initBme();
  readerOnline = initReader();

  printStatus();

  if (readerOnline) {
    beepOk();
  } else {
    Serial.println(F("ERR;RC522_OFFLINE"));
    beepErr();
  }

  if (!bmeOnline) Serial.println(F("ERR;BME_OFFLINE"));

  lastEnvMs    = millis();
  lastHealthMs = millis();

#if USE_WATCHDOG
  wdt_enable(WDTO_4S);
#endif
}

// ---------- Fociklus ----------
void loop() {
#if USE_WATCHDOG
  wdt_reset();
#endif

  uint32_t now = millis();

  serviceLeds(now);
  handleSerialCommands();

  // --- Kornyezeti meres ---
  // A millis() tulcsordulasa (~49 nap) a kivonas miatt helyesen kezelt.
  if (now - lastEnvMs >= ENV_INTERVAL_MS) {
    lastEnvMs = now;
    if (bmeOnline) {
      sendEnv();
    } else if (initBme()) {
      bmeOnline = true;
      Serial.println(F("OK;BME_RECOVERED"));
      sendEnv();
    }
  }

  // --- RFID olvaso eletjel-ellenorzes ---
  if (now - lastHealthMs >= HEALTHCHECK_MS) {
    lastHealthMs = now;
    byte v = readVersion();
    if (v == 0x00 || v == 0xFF) {
      if (readerOnline) {
        Serial.println(F("ERR;RC522_OFFLINE"));
        beepErr();
      }
      readerOnline = false;
      if (initReader()) {
        readerOnline = true;
        Serial.println(F("OK;RC522_RECOVERED"));
      }
    } else if (!readerOnline) {
      readerOnline = true;
      Serial.println(F("OK;RC522_RECOVERED"));
    }
  }

  if (!readerOnline) return;

  // --- Kartyaolvasas ---
  if (!mfrc522.PICC_IsNewCardPresent()) return;
  if (!mfrc522.PICC_ReadCardSerial())   return;

  byte size = mfrc522.uid.size;
  if (size == 0 || size > 10) {
    mfrc522.PICC_HaltA();
    mfrc522.PCD_StopCrypto1();
    return;
  }

  char uidHex[21];
  uidToHex(mfrc522.uid.uidByte, size, uidHex);

  bool isDuplicate = sameAsLast(mfrc522.uid.uidByte, size) &&
                     (now - lastReadMs < DEBOUNCE_MS);

  if (isDuplicate) {
    Serial.print(F("DUP;"));
    Serial.println(uidHex);
    beepDup();
    // A lastReadMs-t NEM frissitjuk: az olvasoban felejtett kartya igy a
    // debounce ablak letelte utan ujra ervenyes olvasas lesz.
  } else {
    Serial.print(F("CARD;"));
    Serial.println(uidHex);
    rememberUid(mfrc522.uid.uidByte, size);
    beepOk();
  }

  mfrc522.PICC_HaltA();
  mfrc522.PCD_StopCrypto1();
}
