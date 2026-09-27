/*
  Cure oven controller: Toray 3960 cure cycle

  Serial1 (UART on pins D0 = RX, D1 = TX; 5 V logic, cross TX/RX, share GND):
  115200 baud, newline terminated. Nothing is sent over the USB port.
  Every line the board sends starts
  with a tag so a computer can parse it (cure_gui.py does this):

    DATA   ...   once per second: temps, state, timing, heater, raw sensor
    PARAMS ...   current cure parameters (sent at boot and on change)
    EVENT  ...   state changes, faults, pause/resume, vacuum-safe notice
    OK / ERR     reply to a command
    INFO         anything else human-readable

  Commands (case-insensitive, send HELP for the list):
    START               begin a new cure from t = 0
    GOTO <min>          jump to <min> minutes into the cycle (replaces flashTime)
    PAUSE / RESUME      freeze / unfreeze the cycle clock (heater holds setpoint)
    ABORT               heater off, end the cure
    CLEAR               clear a latched fault
    SET <name> <value>  change a parameter (times in min, temps in C)
    DEFAULTS            restore the Toray 3960 defaults
    PARAMS / STATUS     print parameters / print one DATA line now

  Cure progress and parameters are saved to EEPROM, so if the board resets
  mid-cure (power blip, brownout, reset button) it resumes where it left off
  instead of restarting. See README.md for full behavior and fault handling.

  Toray 3960:
    Ramp ~25C -> 120C at ~2.5 C/min, hold 120C for 240 min
    Ramp 120C -> 180C at 2.5 C/min, hold 180C for 120 min
    Maintain vacuum during cure. Do not release vacuum until part is <= 65C.
*/

#include <Adafruit_GFX.h>
#include <Adafruit_ILI9341.h>
#include <SPI.h>
#include <EEPROM.h>
#include <Arduino.h>

// ---------------- Build options ----------------

// 0: on power-up, wait for START (from the GUI or a terminal on Serial1), unless a
//    cure was already in progress, in which case it resumes automatically.
// 1: on power-up, start a new cure if none was in progress (old behavior).
#define AUTO_START 0

// ---------------- Hardware ----------------

#define TFT_CS    10
#define TFT_RST   8
#define TFT_DC    9
Adafruit_ILI9341 tft = Adafruit_ILI9341(TFT_CS, TFT_DC, TFT_RST);   // 240x320 ILI9341

const int thermistorPin = A0;
const int relayPin = 2;

// Thermistor (Steinhart-Hart)
const float Vcc = 5.061;
const float R2 = 2402;
const float A = 0.00045421;
const float B = 0.00035529;
const float C = -0.00000027612;
const uint8_t ADC_SAMPLES = 8;          // readings averaged per measurement

// ---------------- Safety ----------------

const float SENSOR_MIN_C = -20.0;       // below this: thermistor open / unplugged
const float SENSOR_MAX_C = 300.0;       // above this: thermistor shorted
const uint8_t SENSOR_FAULT_COUNT = 3;   // consecutive bad reads before faulting
const float VACUUM_SAFE_C = 65.0;

// ---------------- Timing ----------------

const unsigned long CONTROL_PERIOD_MS = 1000;
const unsigned long SAVE_PERIOD_MS = 30000;

// ---------------- Parameters ----------------

struct Params {
  float ramp1Min;
  float hold1Temp;
  float hold1Min;
  float ramp2Min;
  float hold2Temp;
  float hold2Min;
  float hyst;
  float maxTemp;     // over-temperature cutoff (latched fault)
};

const Params DEFAULT_PARAMS = {38.0, 120.0, 240.0, 24.0, 180.0, 120.0, 2.0, 200.0};
Params P = DEFAULT_PARAMS;

struct ParamDef {
  const char* name;
  float* value;
  float minVal;
  float maxVal;
};

const ParamDef PARAM_DEFS[] = {
  {"ramp1",     &P.ramp1Min,  0.0, 1440.0},
  {"hold1temp", &P.hold1Temp, 0.0, 250.0},
  {"hold1",     &P.hold1Min,  0.0, 1440.0},
  {"ramp2",     &P.ramp2Min,  0.0, 1440.0},
  {"hold2temp", &P.hold2Temp, 0.0, 250.0},
  {"hold2",     &P.hold2Min,  0.0, 1440.0},
  {"hyst",      &P.hyst,      0.1, 20.0},
  {"maxtemp",   &P.maxTemp,   50.0, 280.0},
};
const uint8_t NUM_PARAMS = sizeof(PARAM_DEFS) / sizeof(PARAM_DEFS[0]);

// ---------------- EEPROM layout ----------------
// Header is written rarely (start/stop/param change). Progress is written
// every 30 s, rotated across 32 slots so no single EEPROM cell wears out.

const uint16_t EEPROM_MAGIC = 0xC3A2;

struct Header {
  uint16_t magic;
  Params p;
  uint8_t running;
  float startTemp;
};

struct Progress {
  uint32_t seq;
  uint32_t cycleMs;
};

const int HEADER_ADDR = 0;
const int PROGRESS_ADDR = 64;
const uint8_t PROGRESS_SLOTS = 32;
static_assert(sizeof(Header) <= PROGRESS_ADDR, "Header overlaps progress slots");

// ---------------- State ----------------

enum State : uint8_t { IDLE, RAMP1, HOLD1, RAMP2, HOLD2, COMPLETE };
const char* const STATE_NAMES[] = {"IDLE", "RAMP1", "HOLD1", "RAMP2", "HOLD2", "COMPLETE"};

struct Phase {
  State state;
  float setPoint;
  float startS;      // cycle time this phase began (s)
  float endS;        // cycle time this phase ends (s)
};

State phase = IDLE;
bool running = false;       // a cure is in progress (possibly paused/faulted)
bool paused = false;
bool faulted = false;
const __FlashStringHelper* faultReason = nullptr;
uint8_t sensorBadCount = 0;

unsigned long cycleMs = 0;  // time into the cure cycle; only advances while running
unsigned long lastTickMs = 0;
unsigned long lastControlMs = 0;
unsigned long lastSaveMs = 0;
uint32_t progressSeq = 0;

float startTemp = 25.0;     // temperature when the cure started (ramp 1 origin)
float currentTemp = NAN;
float setPoint = 0.0;
float lastAdc = 0.0;
float lastR = 0.0;
bool heaterOn = false;
bool vacuumSafeAnnounced = false;
bool vacuumSafe = false;

char rxBuf[48];
uint8_t rxLen = 0;

// ---------------- Helpers ----------------

float totalS() {
  return (P.ramp1Min + P.hold1Min + P.ramp2Min + P.hold2Min) * 60.0;
}

Phase phaseAt(float t) {
  float r1 = P.ramp1Min * 60.0;
  float h1 = P.hold1Min * 60.0;
  float r2 = P.ramp2Min * 60.0;
  float h2 = P.hold2Min * 60.0;

  float a = 0;
  if (t < a + r1) return {RAMP1, startTemp + (P.hold1Temp - startTemp) * ((t - a) / r1), a, a + r1};
  a += r1;
  if (t < a + h1) return {HOLD1, P.hold1Temp, a, a + h1};
  a += h1;
  if (t < a + r2) return {RAMP2, P.hold1Temp + (P.hold2Temp - P.hold1Temp) * ((t - a) / r2), a, a + r2};
  a += r2;
  if (t < a + h2) return {HOLD2, P.hold2Temp, a, a + h2};
  a += h2;
  return {COMPLETE, 0.0, a, a};
}

float readTemperature() {
  long sum = 0;
  for (uint8_t i = 0; i < ADC_SAMPLES; i++) {
    sum += analogRead(thermistorPin);
  }
  lastAdc = sum / (float)ADC_SAMPLES;

  if (lastAdc < 1.0) {        // open circuit: avoid divide by zero
    lastR = INFINITY;
    return NAN;
  }

  float V = 5.0 * lastAdc / 1024.0;
  if (V >= Vcc) {             // shorted
    lastR = 0;
    return NAN;
  }

  lastR = R2 * (Vcc - V) / V;
  float lnR = log(lastR);
  return 1.0 / (A + B * lnR + C * lnR * lnR * lnR) - 273.15;
}

bool tempValid(float t) {
  return !isnan(t) && t > SENSOR_MIN_C && t < SENSOR_MAX_C;
}

void setHeater(bool on) {
  heaterOn = on;
  digitalWrite(relayPin, on ? HIGH : LOW);
}

void printState() {
  Serial1.print(STATE_NAMES[phase]);
}

// ---------------- EEPROM ----------------

void saveHeader() {
  Header h;
  h.magic = EEPROM_MAGIC;
  h.p = P;
  h.running = running ? 1 : 0;
  h.startTemp = startTemp;
  EEPROM.put(HEADER_ADDR, h);
}

void saveProgress() {
  progressSeq++;
  Progress pr = {progressSeq, (uint32_t)cycleMs};
  EEPROM.put(PROGRESS_ADDR + (progressSeq % PROGRESS_SLOTS) * sizeof(Progress), pr);
}

// Returns true if a cure was in progress and should resume.
bool loadFromEeprom() {
  Header h;
  EEPROM.get(HEADER_ADDR, h);

  if (h.magic != EEPROM_MAGIC) {
    Serial1.println(F("INFO EEPROM empty or old layout, loading defaults"));
    P = DEFAULT_PARAMS;
    running = false;
    Progress zero = {0, 0};
    for (uint8_t i = 0; i < PROGRESS_SLOTS; i++) {
      EEPROM.put(PROGRESS_ADDR + i * sizeof(Progress), zero);
    }
    progressSeq = 0;
    saveHeader();
    return false;
  }

  P = h.p;
  startTemp = h.startTemp;

  progressSeq = 0;
  unsigned long savedMs = 0;
  for (uint8_t i = 0; i < PROGRESS_SLOTS; i++) {
    Progress pr;
    EEPROM.get(PROGRESS_ADDR + i * sizeof(Progress), pr);
    if (pr.seq >= progressSeq) {
      progressSeq = pr.seq;
      savedMs = pr.cycleMs;
    }
  }

  if (h.running) {
    running = true;
    cycleMs = savedMs;
    return true;
  }
  return false;
}

// ---------------- Screen ----------------

const float T_AXIS_MIN = 20.0;

int timeToX(float s) {
  float tot = totalS();
  if (tot <= 0) return 10;
  float x = 10 + (s / tot) * 300.0;
  return constrain((int)round(x), 10, 310);
}

int tempToY(float t) {
  float span = P.hold2Temp - T_AXIS_MIN;
  if (span <= 0) span = 1;
  float y = 230 - ((t - T_AXIS_MIN) / span) * 200.0;
  return constrain((int)round(y), 25, 235);
}

void drawProfile() {
  tft.fillScreen(ILI9341_BLACK);
  tft.setTextColor(ILI9341_WHITE);
  tft.setTextSize(2);
  tft.setCursor(10, 5);
  tft.print(F("Cure Cycle"));

  float r1 = P.ramp1Min * 60, h1 = P.hold1Min * 60, r2 = P.ramp2Min * 60;
  int x1 = timeToX(0);
  int x2 = timeToX(r1);
  int x3 = timeToX(r1 + h1);
  int x4 = timeToX(r1 + h1 + r2);
  int x5 = timeToX(totalS());
  int y1 = tempToY(startTemp);
  int y2 = tempToY(P.hold1Temp);
  int y3 = tempToY(P.hold2Temp);

  tft.drawLine(x1, y1, x2, y2, ILI9341_WHITE);
  tft.drawLine(x2, y2, x3, y2, ILI9341_WHITE);
  tft.drawLine(x3, y2, x4, y3, ILI9341_WHITE);
  tft.drawLine(x4, y3, x5, y3, ILI9341_WHITE);
}

void drawScreen() {
  if (running && tempValid(currentTemp)) {
    tft.drawPixel(timeToX(cycleMs / 1000.0), tempToY(currentTemp), ILI9341_RED);
  }

  tft.setTextSize(2);
  tft.setTextColor(ILI9341_WHITE);
  tft.fillRect(205, 125, 115, 100, ILI9341_BLACK);

  tft.setCursor(125, 125); tft.print(F("Temp:"));
  tft.setCursor(205, 125);
  if (tempValid(currentTemp)) tft.print(currentTemp); else tft.print(F("ERR"));

  tft.setCursor(125, 150); tft.print(F("Set:"));
  tft.setCursor(205, 150); tft.print(setPoint);

  tft.setCursor(125, 175); tft.print(F("Time:"));
  tft.setCursor(205, 175); tft.print(cycleMs / 60000.0);

  tft.setCursor(125, 200); tft.print(F("State:"));
  tft.setCursor(205, 200);
  if (faulted)      { tft.setTextColor(ILI9341_RED);    tft.print(F("FAULT")); }
  else if (paused)  { tft.setTextColor(ILI9341_YELLOW); tft.print(F("PAUSED")); }
  else              { tft.print(STATE_NAMES[phase]); }
}

// ---------------- Serial output ----------------

void printParams() {
  Serial1.print(F("PARAMS"));
  for (uint8_t i = 0; i < NUM_PARAMS; i++) {
    Serial1.print(' ');
    Serial1.print(PARAM_DEFS[i].name);
    Serial1.print('=');
    Serial1.print(*PARAM_DEFS[i].value, 2);
  }
  Serial1.print(F(" start="));
  Serial1.print(startTemp, 2);
  Serial1.print(F(" total="));
  Serial1.println(totalS() / 60.0, 2);
}

void printData() {
  Phase ph = phaseAt(cycleMs / 1000.0);
  float t = cycleMs / 1000.0;
  bool inCure = running && phase != COMPLETE;

  Serial1.print(F("DATA t="));        Serial1.print(t, 1);
  Serial1.print(F(" state="));        printState();
  Serial1.print(F(" paused="));       Serial1.print(paused ? 1 : 0);
  Serial1.print(F(" fault="));        Serial1.print(faulted ? 1 : 0);
  Serial1.print(F(" temp="));         Serial1.print(currentTemp, 2);
  Serial1.print(F(" set="));          Serial1.print(setPoint, 2);
  Serial1.print(F(" err="));          Serial1.print(setPoint - currentTemp, 2);
  Serial1.print(F(" heater="));       Serial1.print(heaterOn ? 1 : 0);
  Serial1.print(F(" st="));           Serial1.print(inCure ? t - ph.startS : 0, 0);
  Serial1.print(F(" srem="));         Serial1.print(inCure ? ph.endS - t : 0, 0);
  Serial1.print(F(" rem="));          Serial1.print(inCure ? totalS() - t : 0, 0);
  Serial1.print(F(" vac="));          Serial1.print(vacuumSafe ? 1 : 0);
  Serial1.print(F(" adc="));          Serial1.print(lastAdc, 1);
  Serial1.print(F(" R="));            Serial1.print(lastR, 1);
  Serial1.print(F(" up="));           Serial1.println(millis() / 1000);
}

void printHelp() {
  Serial1.println(F("INFO commands: START | GOTO <min> | PAUSE | RESUME | ABORT | CLEAR"));
  Serial1.println(F("INFO           SET <name> <value> | DEFAULTS | PARAMS | STATUS | HELP"));
  Serial1.println(F("INFO params:   ramp1 hold1 ramp2 hold2 (min), hold1temp hold2temp hyst maxtemp (C)"));
}

// ---------------- Run control ----------------

void raiseFault(const __FlashStringHelper* reason) {
  setHeater(false);
  if (!faulted) {
    faulted = true;
    faultReason = reason;
    Serial1.print(F("EVENT fault reason="));
    Serial1.print(reason);
    Serial1.print(F(" temp="));
    Serial1.println(currentTemp, 2);
  }
}

// Returns false (and changes nothing) if the start temperature can't be read.
bool startRun(float atMin) {
  float t = readTemperature();
  if (atMin <= 0 || !running) {
    if (!tempValid(t)) {
      Serial1.println(F("ERR cannot start: thermistor reading invalid"));
      return false;
    }
    startTemp = t;
  }
  phase = IDLE;   // so the next update reports the phase we land in
  running = true;
  paused = false;
  faulted = false;
  sensorBadCount = 0;
  vacuumSafe = false;
  vacuumSafeAnnounced = false;
  cycleMs = (unsigned long)(atMin * 60000.0);
  saveHeader();
  saveProgress();

  Serial1.print(F("EVENT start t="));
  Serial1.print(cycleMs / 1000.0, 1);
  Serial1.print(F(" start="));
  Serial1.println(startTemp, 2);
  printParams();
  drawProfile();
  return true;
}

void finishRun() {
  running = false;
  paused = false;
  setHeater(false);
  saveHeader();
  Serial1.println(F("EVENT complete"));
}

void abortRun() {
  running = false;
  paused = false;
  phase = IDLE;
  setPoint = 0;
  setHeater(false);
  saveHeader();
  Serial1.println(F("EVENT abort"));
}

void updatePhase() {
  if (!running) {
    setPoint = 0;
    return;
  }
  Phase ph = phaseAt(cycleMs / 1000.0);
  setPoint = ph.setPoint;
  if (ph.state != phase) {
    Serial1.print(F("EVENT state from="));
    printState();
    Serial1.print(F(" to="));
    Serial1.print(STATE_NAMES[ph.state]);
    Serial1.print(F(" t="));
    Serial1.println(cycleMs / 1000.0, 1);
    phase = ph.state;
    if (phase == COMPLETE) finishRun();
  }
}

void controlHeating() {
  if (!heaterOn && currentTemp < setPoint - P.hyst) {
    setHeater(true);
  } else if (heaterOn && currentTemp > setPoint + P.hyst) {
    setHeater(false);
  }
}

void controlStep() {
  float t = readTemperature();
  if (tempValid(t)) {
    currentTemp = t;
    sensorBadCount = 0;
  } else {
    currentTemp = t;
    if (sensorBadCount < 255) sensorBadCount++;
    if (sensorBadCount >= SENSOR_FAULT_COUNT) raiseFault(F("sensor"));
  }

  if (tempValid(currentTemp) && currentTemp > P.maxTemp) raiseFault(F("overtemp"));

  updatePhase();

  if (running && !faulted && phase != COMPLETE && tempValid(currentTemp)) {
    controlHeating();
  } else {
    setHeater(false);
  }

  vacuumSafe = (phase == COMPLETE && tempValid(currentTemp) && currentTemp <= VACUUM_SAFE_C);
  if (vacuumSafe && !vacuumSafeAnnounced) {
    vacuumSafeAnnounced = true;
    Serial1.print(F("EVENT vacuum_safe temp="));
    Serial1.println(currentTemp, 2);
  }

  printData();
  drawScreen();
}

// ---------------- Commands ----------------

bool parseFloat(const char* s, float& out) {
  if (!s) return false;
  char* end;
  out = strtod(s, &end);
  return end != s && *end == '\0';
}

void setParam(const char* name, const char* valueStr) {
  float v;
  if (!name || !parseFloat(valueStr, v)) {
    Serial1.println(F("ERR usage: SET <name> <value>"));
    return;
  }
  for (uint8_t i = 0; i < NUM_PARAMS; i++) {
    if (strcasecmp(name, PARAM_DEFS[i].name) == 0) {
      if (v < PARAM_DEFS[i].minVal || v > PARAM_DEFS[i].maxVal) {
        Serial1.print(F("ERR "));
        Serial1.print(PARAM_DEFS[i].name);
        Serial1.print(F(" must be between "));
        Serial1.print(PARAM_DEFS[i].minVal);
        Serial1.print(F(" and "));
        Serial1.println(PARAM_DEFS[i].maxVal);
        return;
      }
      *PARAM_DEFS[i].value = v;
      saveHeader();
      Serial1.print(F("OK set "));
      Serial1.print(PARAM_DEFS[i].name);
      Serial1.print('=');
      Serial1.println(v, 2);
      if (P.maxTemp <= P.hold2Temp) {
        Serial1.println(F("INFO warning: maxtemp is at or below hold2temp, cure will fault"));
      }
      printParams();
      drawProfile();
      return;
    }
  }
  Serial1.print(F("ERR unknown parameter: "));
  Serial1.println(name);
}

void handleCommand(char* line) {
  char* cmd = strtok(line, " \t");
  if (!cmd) return;
  char* arg1 = strtok(NULL, " \t");
  char* arg2 = strtok(NULL, " \t");

  if (strcasecmp(cmd, "HELP") == 0) {
    printHelp();
  } else if (strcasecmp(cmd, "STATUS") == 0) {
    printData();
  } else if (strcasecmp(cmd, "PARAMS") == 0) {
    printParams();
  } else if (strcasecmp(cmd, "START") == 0) {
    if (startRun(0)) Serial1.println(F("OK start"));
  } else if (strcasecmp(cmd, "GOTO") == 0) {
    float m;
    if (!parseFloat(arg1, m) || m < 0 || m * 60.0 > totalS()) {
      Serial1.println(F("ERR usage: GOTO <minutes>, within the cycle length"));
      return;
    }
    if (startRun(m)) {
      Serial1.print(F("OK goto "));
      Serial1.println(m, 2);
    }
  } else if (strcasecmp(cmd, "PAUSE") == 0) {
    if (!running) { Serial1.println(F("ERR not running")); return; }
    paused = true;
    saveProgress();
    Serial1.println(F("EVENT pause"));
    Serial1.println(F("OK pause"));
  } else if (strcasecmp(cmd, "RESUME") == 0) {
    if (!running) { Serial1.println(F("ERR not running")); return; }
    paused = false;
    Serial1.println(F("EVENT resume"));
    Serial1.println(F("OK resume"));
  } else if (strcasecmp(cmd, "ABORT") == 0) {
    abortRun();
    Serial1.println(F("OK abort"));
  } else if (strcasecmp(cmd, "CLEAR") == 0) {
    faulted = false;
    sensorBadCount = 0;
    Serial1.println(F("EVENT fault_cleared"));
    Serial1.println(F("OK clear"));
  } else if (strcasecmp(cmd, "SET") == 0) {
    setParam(arg1, arg2);
  } else if (strcasecmp(cmd, "DEFAULTS") == 0) {
    P = DEFAULT_PARAMS;
    saveHeader();
    Serial1.println(F("OK defaults"));
    printParams();
    drawProfile();
  } else {
    Serial1.print(F("ERR unknown command: "));
    Serial1.println(cmd);
  }
}

void pollSerial() {
  while (Serial1.available()) {
    char c = Serial1.read();
    if (c == '\n' || c == '\r') {
      if (rxLen > 0) {
        rxBuf[rxLen] = '\0';
        handleCommand(rxBuf);
        rxLen = 0;
      }
    } else if (rxLen < sizeof(rxBuf) - 1) {
      rxBuf[rxLen++] = c;
    }
  }
}

// ---------------- Arduino entry points ----------------

void setup() {
  pinMode(relayPin, OUTPUT);
  setHeater(false);                 // heater off before anything else

  pinMode(thermistorPin, INPUT);
  digitalWrite(TFT_CS, HIGH);
  pinMode(TFT_CS, OUTPUT);

  Serial1.begin(115200);
  Serial1.println(F("INFO cure controller boot"));

  tft.begin();
  tft.setRotation(3);                // landscape 320x240; use 1 if the image is upside down

  bool resume = loadFromEeprom();
  currentTemp = readTemperature();
  printParams();

  if (resume) {
    Serial1.print(F("EVENT resumed t="));
    Serial1.println(cycleMs / 1000.0, 1);
  } else if (AUTO_START) {
    startRun(0);
  } else {
    Serial1.println(F("INFO idle, send START to begin a cure"));
  }

  drawProfile();
  lastTickMs = lastControlMs = lastSaveMs = millis();
}

void loop() {
  pollSerial();

  unsigned long now = millis();
  if (running && !paused && !faulted) {
    cycleMs += now - lastTickMs;
  }
  lastTickMs = now;

  if (now - lastControlMs >= CONTROL_PERIOD_MS) {
    lastControlMs += CONTROL_PERIOD_MS;
    if (now - lastControlMs >= CONTROL_PERIOD_MS) lastControlMs = now;  // fell behind
    controlStep();
  }

  if (running && now - lastSaveMs >= SAVE_PERIOD_MS) {
    lastSaveMs = now;
    saveProgress();
  }
}
