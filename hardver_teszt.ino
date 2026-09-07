/*
 * HARDVER TESZT - RFID munkaido-nyilvantarto vegpont
 * ---------------------------------------------------
 * Ezt a sketchet toltsd fel ELOSZOR, forrasztas utan.
 * Vegigmeri az osszes modult es megmondja, mi hianyzik vagy mi rossz.
 *
 * HASZNALAT
 *   1. Feltoltes utan nyisd meg a Soros monitort
 *   2. Allitsd 115200 baudra es "Both NL & CR" sorvegre
 *   3. A teszt automatikusan lefut, majd parancsokra vár
 *
 * PARANCSOK (ird be a soros monitor felso mezojebe, Enter)
 *   h   sugo
 *   t   teljes teszt ujra
 *   s   TERHELESES TESZT (30 mp) - EZ MUTATJA MEG A HIDEGFORRASZTAST
 *   b   buzzer teszt
 *   l   LED teszt
 *   e   BME280 meres
 *   i   I2C busz vegigpasztazasa
 *   c   folyamatos kartyaolvasas be/ki
 *
 * MEGJEGYZES: a kimenet szandekosan ekezet nelkuli, mert a regebbi
 * Arduino IDE soros monitora osszekutyulja az UTF-8 karaktereket.
 *
 * BEKOTES (ugyanaz, mint az eles sketchben)
 *   RC522:  3V3->3.3V  D9->RST  GND->GND  D12->MISO  D11->MOSI  D13->SCK  D10->SDA
 *   BME280: 3V3->VIN   GND->GND  A4->SDA  A5->SCL
 *   Buzzer: D6 -> (+), masik lab GND
 *   LED-ek: D5 -> zold + 220ohm -> GND,  D4 -> piros + 220ohm -> GND
 */

#include <SPI.h>
#include <Wire.h>
#include <MFRC522.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_BME280.h>

#define RST_PIN     9
#define SS_PIN      10
#define BUZZER_PIN  6
#define LED_OK_PIN  5
#define LED_ERR_PIN 4

MFRC522 mfrc522(SS_PIN, RST_PIN);
Adafruit_BME280 bme;

byte bmeAddr    = 0;
byte rc522Ver   = 0;
bool cardMode   = true;
uint32_t lastCardMs = 0;

// ---------------------------------------------------------------
// Kiirasi segedek
// ---------------------------------------------------------------
void line() { Serial.println(F("------------------------------------------")); }

void title(const __FlashStringHelper *s) {
  Serial.println();
  Serial.print(F("== ")); Serial.println(s);
}

void ok(const __FlashStringHelper *s)   { Serial.print(F("  [OK]   ")); Serial.println(s); }
void bad(const __FlashStringHelper *s)  { Serial.print(F("  [HIBA] ")); Serial.println(s); }
void warn(const __FlashStringHelper *s) { Serial.print(F("  [!]    ")); Serial.println(s); }
void hint(const __FlashStringHelper *s) { Serial.print(F("         -> ")); Serial.println(s); }

// ---------------------------------------------------------------
// 1. LED es buzzer
// ---------------------------------------------------------------
void testFeedback() {
  title(F("1. LED es buzzer"));
  Serial.println(F("  Figyeld az eszkozt 4 masodpercig."));

  Serial.println(F("  ... zold LED"));
  digitalWrite(LED_OK_PIN, HIGH);  delay(700); digitalWrite(LED_OK_PIN, LOW);
  delay(300);

  Serial.println(F("  ... piros LED"));
  digitalWrite(LED_ERR_PIN, HIGH); delay(700); digitalWrite(LED_ERR_PIN, LOW);
  delay(300);

  Serial.println(F("  ... buzzer: harom emelkedo hang"));
  tone(BUZZER_PIN, 1200, 150); delay(220);
  tone(BUZZER_PIN, 1800, 150); delay(220);
  tone(BUZZER_PIN, 2400, 250); delay(350);

  Serial.println(F("  Ha nem lattad/hallottad valamelyiket:"));
  hint(F("LED: forditva van bekotve? A rovid lab megy GND-re."));
  hint(F("Buzzer: PASSZIV kell, az aktiv nem szol tone()-ra."));
}

// ---------------------------------------------------------------
// 2. SPI busz es RC522
// ---------------------------------------------------------------
void testRc522() {
  title(F("2. SPI busz es RC522 olvaso"));

  mfrc522.PCD_Init();
  delay(50);
  rc522Ver = mfrc522.PCD_ReadRegister(MFRC522::VersionReg);

  Serial.print(F("  VersionReg = 0x"));
  if (rc522Ver < 0x10) Serial.print('0');
  Serial.println(rc522Ver, HEX);

  if (rc522Ver == 0x00 || rc522Ver == 0xFF) {
    bad(F("Az RC522 nem valaszol."));
    hint(F("Ez a leggyakoribb hiba. Ellenorizd sorban:"));
    hint(F("1. Kap-e 3,3 V-ot? Merd meg a modul labain."));
    hint(F("2. Nincs osszekeverve a MISO (D12) es MOSI (D11)?"));
    hint(F("3. Az SDA/SS a D10-en van? Nem a D9-en?"));
    hint(F("4. Van kozos GND?"));
    hint(F("5. Hidegforrasztas? Nezd meg nagyitoval, matt-e a forrasz."));
    return;
  }

  switch (rc522Ver) {
    case 0x88: ok(F("Valaszol. Tipus: FM17522 kloon (mukodik).")); break;
    case 0x90: ok(F("Valaszol. Tipus: MFRC522 v0.0")); break;
    case 0x91: ok(F("Valaszol. Tipus: MFRC522 v1.0")); break;
    case 0x92: ok(F("Valaszol. Tipus: MFRC522 v2.0")); break;
    case 0xB2: ok(F("Valaszol. Tipus: FM17522E kloon (mukodik).")); break;
    default:
      warn(F("Valaszol, de ismeretlen verzio. Valoszinuleg kloon, jo lesz."));
      break;
  }

  // A konyvtar belso onteszte. Ez oda-vissza igazolja az SPI buszt
  // es a chip belso mukodeset - sokkal tobbet er, mint egy regiszter-olvasas.
  Serial.println(F("  ... belso onteszt fut"));
  bool selfTest = mfrc522.PCD_PerformSelfTest();
  if (selfTest) ok(F("Onteszt sikeres. Az SPI busz megbizhato."));
  else {
    warn(F("Onteszt nem ment at."));
    hint(F("Kloon lapkaknal ez normalis lehet, ha a verzio olvashato volt."));
    hint(F("Eredeti chipnel viszont instabil SPI-t jelent (kabel, forrasztas)."));
  }

  // Az onteszt utan a chipet ujra kell inicializalni.
  mfrc522.PCD_Init();
  mfrc522.PCD_AntennaOn();

  byte gain = mfrc522.PCD_GetAntennaGain();
  Serial.print(F("  Antenna erositese: 0x"));
  Serial.println(gain, HEX);
}

// ---------------------------------------------------------------
// 3. I2C busz
// ---------------------------------------------------------------
byte scanI2c(bool verbose) {
  if (verbose) title(F("3. I2C busz vegigpasztazasa"));
  byte found = 0;
  for (byte addr = 1; addr < 127; addr++) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      found++;
      if (verbose) {
        Serial.print(F("  Eszkoz talalva: 0x"));
        if (addr < 0x10) Serial.print('0');
        Serial.print(addr, HEX);
        if (addr == 0x76 || addr == 0x77) Serial.print(F("  <- ez lehet a BME280"));
        Serial.println();
      }
    }
  }
  if (verbose) {
    if (found == 0) {
      bad(F("Egyetlen I2C eszkoz sincs a buszon."));
      hint(F("1. Nincs felcserelve az SDA (A4) es az SCL (A5)?"));
      hint(F("2. Kap-e tapot a modul? Merd meg a VIN/VCC labat."));
      hint(F("3. Van kozos GND?"));
      hint(F("4. Hidegforrasztas a szenzor labain?"));
    } else {
      Serial.print(F("  Osszesen "));
      Serial.print(found);
      Serial.println(F(" eszkoz."));
    }
  }
  return found;
}

// ---------------------------------------------------------------
// 4. BME280
// ---------------------------------------------------------------
bool initBme() {
  if      (bme.begin(0x76, &Wire)) bmeAddr = 0x76;
  else if (bme.begin(0x77, &Wire)) bmeAddr = 0x77;
  else { bmeAddr = 0; return false; }

  bme.setSampling(Adafruit_BME280::MODE_FORCED,
                  Adafruit_BME280::SAMPLING_X1,
                  Adafruit_BME280::SAMPLING_X1,
                  Adafruit_BME280::SAMPLING_X1,
                  Adafruit_BME280::FILTER_OFF);
  return true;
}

void readBme(bool verbose) {
  if (!bmeAddr) {
    if (verbose) bad(F("Nincs inicializalt BME280."));
    return;
  }

  bme.takeForcedMeasurement();
  float t = bme.readTemperature();
  float h = bme.readHumidity();
  float p = bme.readPressure() / 100.0F;

  char b[12];
  Serial.print(F("  Homerseklet : ")); dtostrf(t, 0, 2, b); Serial.print(b); Serial.println(F(" C"));
  Serial.print(F("  Paratartalom: ")); dtostrf(h, 0, 2, b); Serial.print(b); Serial.println(F(" %"));
  Serial.print(F("  Legnyomas   : ")); dtostrf(p, 0, 2, b); Serial.print(b); Serial.println(F(" hPa"));

  if (!verbose) return;

  bool sane = true;
  if (isnan(t) || t < -40 || t > 85)    { bad(F("A homerseklet ertelmetlen.")); sane = false; }
  if (isnan(h) || h < 0   || h > 100)   { bad(F("A paratartalom ertelmetlen.")); sane = false; }
  if (isnan(p) || p < 300 || p > 1100)  { bad(F("A legnyomas ertelmetlen.")); sane = false; }

  if (h == 0.0F) {
    warn(F("A paratartalom pontosan 0 - ez valoszinuleg BMP280, nem BME280."));
    hint(F("A BMP nem tud paratartalmat merni. Masik modul kell."));
    sane = false;
  }

  if (sane) {
    ok(F("Az ertekek hihetoek."));
    hint(F("Vesd ossze egy szobahomerovel. Ha 2-3 fokkal tobbet mutat,"));
    hint(F("a szenzor tul kozel van melegedo alkatreszhez."));
  }
}

void testBme() {
  title(F("4. BME280 szenzor"));
  if (initBme()) {
    Serial.print(F("  Cim: 0x")); Serial.println(bmeAddr, HEX);
    ok(F("A szenzor valaszol."));
    delay(100);
    readBme(true);
  } else {
    bad(F("A BME280 nem talalhato sem 0x76, sem 0x77 cimen."));
    if (scanI2c(false) > 0) {
      hint(F("Van masik eszkoz a buszon - nezd meg az 'i' paranccsal."));
      hint(F("Lehet, hogy BMP280-at vettel BME280 helyett."));
    } else {
      hint(F("Az I2C busz teljesen nema. Lasd a 3. tesztet."));
    }
  }
}

// ---------------------------------------------------------------
// 5. TERHELESES TESZT - ez mutatja meg a hidegforrasztast
// ---------------------------------------------------------------
void soakTest() {
  title(F("TERHELESES TESZT (30 masodperc)"));
  Serial.println(F("  Masodpercenkent 10x megszolitja mindket modult."));
  Serial.println(F("  Kozben OVATOSAN mozgasd a kabeleket es nyomkodd a"));
  Serial.println(F("  forrasztasokat - igy jon elo a hideg kotes."));
  Serial.println();

  const uint16_t total = 300;
  uint16_t spiErr = 0, i2cErr = 0;
  bool haveBme = (bmeAddr != 0);

  for (uint16_t i = 0; i < total; i++) {
    byte v = mfrc522.PCD_ReadRegister(MFRC522::VersionReg);
    if (v != rc522Ver) spiErr++;

    if (haveBme) {
      Wire.beginTransmission(bmeAddr);
      if (Wire.endTransmission() != 0) i2cErr++;
    }

    if (i % 30 == 29) {
      Serial.print(F("  "));
      Serial.print((i + 1) / 30);
      Serial.print(F("/10  SPI hiba: ")); Serial.print(spiErr);
      if (haveBme) { Serial.print(F("  I2C hiba: ")); Serial.print(i2cErr); }
      Serial.println();
    }
    delay(100);
  }

  Serial.println();
  line();
  Serial.print(F("  SPI (RC522): ")); Serial.print(spiErr);
  Serial.print(F(" hiba / ")); Serial.println(total);
  if (haveBme) {
    Serial.print(F("  I2C (BME280): ")); Serial.print(i2cErr);
    Serial.print(F(" hiba / ")); Serial.println(total);
  }
  line();

  if (spiErr == 0 && i2cErr == 0) {
    ok(F("Nulla hiba. A forrasztas es a tapellatas rendben van."));
    tone(BUZZER_PIN, 2400, 200);
  } else {
    bad(F("Voltak kihagyasok. Ez NEM veletlen."));
    hint(F("1. Hidegforrasztas: forraszd ujra a gyanus pontokat."));
    hint(F("2. Tapellatas: a Nano 3,3 V laba gyenge. Tegy be"));
    hint(F("   egy AMS1117-3.3 modult az 5 V-rol taplalva."));
    hint(F("3. Tul hosszu vagy vekony vezetek, dugaszolo panel."));
    tone(BUZZER_PIN, 400, 500);
  }
}

// ---------------------------------------------------------------
// Kartyaolvasas
// ---------------------------------------------------------------
void pollCard() {
  if (!mfrc522.PICC_IsNewCardPresent()) return;
  if (!mfrc522.PICC_ReadCardSerial())   return;

  Serial.print(F("  KARTYA  UID: "));
  for (byte i = 0; i < mfrc522.uid.size; i++) {
    if (mfrc522.uid.uidByte[i] < 0x10) Serial.print('0');
    Serial.print(mfrc522.uid.uidByte[i], HEX);
  }
  Serial.print(F("  ("));
  Serial.print(mfrc522.uid.size);
  Serial.print(F(" bajt)  tipus: "));
  Serial.println(mfrc522.PICC_GetTypeName(mfrc522.PICC_GetType(mfrc522.uid.sak)));

  digitalWrite(LED_OK_PIN, HIGH);
  tone(BUZZER_PIN, 2200, 90);
  delay(120);
  digitalWrite(LED_OK_PIN, LOW);

  lastCardMs = millis();
  mfrc522.PICC_HaltA();
  mfrc522.PCD_StopCrypto1();
}

// ---------------------------------------------------------------
// Osszegzes es sugo
// ---------------------------------------------------------------
void summary() {
  Serial.println();
  line();
  Serial.println(F("  OSSZEGZES"));
  Serial.print(F("  RC522  : "));
  Serial.println((rc522Ver == 0x00 || rc522Ver == 0xFF) ? F("NEM MUKODIK") : F("rendben"));
  Serial.print(F("  BME280 : "));
  Serial.println(bmeAddr ? F("rendben") : F("NEM TALALHATO"));
  Serial.println(F("  Buzzer/LED: szemre es fulre ellenorizd."));
  line();
  Serial.println();
  Serial.println(F("  KOVETKEZO LEPES: futtasd le a terheleses tesztet ('s')."));
  Serial.println(F("  Az mutatja meg, jol forrasztottal-e."));
  Serial.println();
}

void help() {
  Serial.println();
  Serial.println(F("  PARANCSOK"));
  Serial.println(F("   h  sugo"));
  Serial.println(F("   t  teljes teszt ujra"));
  Serial.println(F("   s  terheleses teszt (30 mp)"));
  Serial.println(F("   b  buzzer + LED"));
  Serial.println(F("   e  BME280 meres"));
  Serial.println(F("   i  I2C pasztazas"));
  Serial.println(F("   c  kartyaolvasas be/ki"));
  Serial.println();
}

void runAll() {
  Serial.println();
  line();
  Serial.println(F("  HARDVER TESZT - RFID vegpont"));
  line();
  testFeedback();
  testRc522();
  scanI2c(true);
  testBme();
  summary();
  help();
}

// ---------------------------------------------------------------
void setup() {
  Serial.begin(115200);
  while (!Serial && millis() < 3000) { }

  pinMode(LED_OK_PIN, OUTPUT);
  pinMode(LED_ERR_PIN, OUTPUT);
  pinMode(BUZZER_PIN, OUTPUT);
  digitalWrite(LED_OK_PIN, LOW);
  digitalWrite(LED_ERR_PIN, LOW);

  SPI.begin();

  Wire.begin();
  // A Wire konyvtar alapbol 5 V-ra huzza fel az I2C vonalakat, ami egy
  // 3,3 V-os szenzort lassan tonkretesz. Ez a ket sor kikapcsolja a
  // belso felhuzokat, igy a modul sajat felhuzoi hataroznak meg.
  digitalWrite(SDA, LOW);
  digitalWrite(SCL, LOW);

  runAll();
  lastCardMs = millis();
}

void loop() {
  if (Serial.available()) {
    char c = Serial.read();
    switch (c) {
      case 't': runAll(); break;
      case 's': soakTest(); break;
      case 'b': testFeedback(); break;
      case 'e': Serial.println(); readBme(true); break;
      case 'i': scanI2c(true); break;
      case 'h': help(); break;
      case 'c':
        cardMode = !cardMode;
        Serial.println(cardMode ? F("  Kartyaolvasas BE") : F("  Kartyaolvasas KI"));
        break;
      default: break;   // sorvegek es egyeb karakterek
    }
  }

  if (cardMode && rc522Ver != 0x00 && rc522Ver != 0xFF) {
    pollCard();

    // Ha 20 masodperce nem volt kartya, emlekeztet.
    if (millis() - lastCardMs > 20000UL) {
      Serial.println(F("  (nincs kartya 20 mp-e - tegy egyet az olvasora)"));
      lastCardMs = millis();
    }
  }
}
