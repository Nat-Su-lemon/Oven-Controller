#include <Adafruit_GFX.h>
#include <Adafruit_ST7789.h>
#include <SPI.h>
#include <Arduino.h>
#include <math.h>

float flashTime = 0 * 60 * 1000; // Change 0 to the minute in the cycle to start at

// Pin definitions
#define TFT_CS  10
#define TFT_RST 8
#define TFT_DC  9

Adafruit_ST7789 tft = Adafruit_ST7789(TFT_CS, TFT_DC, TFT_RST);

const int thermistorPin = A0;
const int relayPin = 2;

// Cure cycle times (seconds) and temperatures (Celsius)
//
// Toray 3960:
// Ramp ~25 C -> 120 C at ~2.5 C/min
// Hold 120 C for 240 min
// Ramp 120 C -> 180 C at 2.5 C/min
// Hold 180 C for 120 min
//
// Maintain vacuum during cure.
// Do not release vacuum until part is <= 65 C.

const float ramp1Time = 38 * 60;
const float hold1Temp = 120.0;
const float hold1Time = 240 * 60;

const float ramp2Time = 24 * 60;
const float hold2Temp = 180.0;
const float hold2Time = 120 * 60;

const float totTime = ramp1Time + hold1Time + ramp2Time + hold2Time;

// Cure cycle graph coordinates
int x_1 = 10;
int x_2 = round(x_1 + (ramp1Time / totTime) * 300);
int x_3 = round(x_2 + (hold1Time / totTime) * 300);
int x_4 = round(x_3 + (ramp2Time / totTime) * 300);
int x_5 = x_1 + 300;

int y_1 = 230;
int y_2 = round(y_1 - (hold1Temp / hold2Temp) * 200);
int y_3 = y_1 - 200;

// Thermistor constants
float Vcc = 5.061;
float R2 = 2402;
float R;
float A = 0.00045421;
float B = 0.00035529;
float C = -0.00000027612;

// Time and temperature
float currentTemperature = 0.0;
float roomTemp = 0.0;
unsigned long elapsedMillis = 0;
unsigned long stateMillis = 0;

// State tracking
enum State { RAMP1, HOLD1, RAMP2, HOLD2, COMPLETE };
State currentState = RAMP1;
unsigned long stateStartMillis = 0;

float hysteresis = 2.0;

const char* getStateName() {
  switch (currentState) {
    case RAMP1:    return "RAMP1";
    case HOLD1:    return "HOLD1";
    case RAMP2:    return "RAMP2";
    case HOLD2:    return "HOLD2";
    case COMPLETE: return "COMPLETE";
    default:       return "UNKNOWN";
  }
}

float readTemperature() {
  float V = 5.0 * analogRead(thermistorPin) / 1024.0;

  R = R2 * (Vcc - V) / V;

  float Temp = 1 / (A + B * log(R) + C * pow(log(R), 3));
  Temp = Temp - 273.15;

  return Temp;
}

float calculateSetPoint(
  unsigned long elapsedSeconds,
  float startTemp,
  float targetTemp,
  int rampTime
) {
  float progress = float(elapsedSeconds) / rampTime;

  if (progress > 1.0) {
    progress = 1.0;
  }

  return startTemp + progress * (targetTemp - startTemp);
}

void writeScreen(float temp, float time, float setPoint) {
  float timeMinutes = time / 60;

  int x0 = round(x_1 + (time / totTime) * 300);
  int y0 = round(y_1 - ((temp - roomTemp) / hold2Temp) * 200);

  tft.drawPixel(x0, y0, ST77XX_RED);

  tft.setTextColor(ST77XX_WHITE);
  tft.setTextSize(2);

  tft.setCursor(10, 10);
  tft.print("Cure Cycle");

  tft.fillRect(180, 125, 120, 100, ST77XX_BLACK);

  tft.setCursor(125, 125);
  tft.print("Temp: ");
  tft.setCursor(205, 125);
  tft.print(temp);

  tft.setCursor(125, 150);
  tft.print("Set: ");
  tft.setCursor(205, 150);
  tft.print(setPoint);

  tft.setCursor(125, 175);
  tft.print("Time: ");
  tft.setCursor(205, 175);
  tft.print(timeMinutes);

  tft.setCursor(125, 200);
  tft.print("State: ");
  tft.setCursor(205, 200);
  tft.print(getStateName());
}

void updateState() {
  stateMillis = (elapsedMillis - stateStartMillis) / 1000;

  switch (currentState) {
    case RAMP1:
      if (stateMillis >= ramp1Time) {
        currentState = HOLD1;
        stateStartMillis = elapsedMillis;
      }
      break;

    case HOLD1:
      if (stateMillis >= hold1Time) {
        currentState = RAMP2;
        stateStartMillis = elapsedMillis;
      }
      break;

    case RAMP2:
      if (stateMillis >= ramp2Time) {
        currentState = HOLD2;
        stateStartMillis = elapsedMillis;
      }
      break;

    case HOLD2:
      if (stateMillis >= hold2Time) {
        currentState = COMPLETE;
        stateStartMillis = elapsedMillis;
        digitalWrite(relayPin, LOW);
      }
      break;

    case COMPLETE:
      digitalWrite(relayPin, LOW);
      break;
  }
}

void controlHeating(float setPoint) {
  if (
    digitalRead(relayPin) == LOW &&
    currentTemperature < (setPoint - hysteresis)
  ) {
    digitalWrite(relayPin, HIGH);
  }
  else if (
    digitalRead(relayPin) == HIGH &&
    currentTemperature > (setPoint + hysteresis)
  ) {
    digitalWrite(relayPin, LOW);
  }
}

void printUARTStatus(float setPoint) {
  Serial1.print("Time (min): ");
  Serial1.print(elapsedMillis / 60000.0, 1);

  Serial1.print(" | Temp (C): ");
  Serial1.print(currentTemperature, 1);

  Serial1.print(" | Setpoint (C): ");
  Serial1.print(setPoint, 1);

  Serial1.print(" | State: ");
  Serial1.print(getStateName());

  Serial1.print(" | Heater: ");
  Serial1.println(digitalRead(relayPin) == HIGH ? "ON" : "OFF");
}

void setup() {
  pinMode(thermistorPin, INPUT);

  pinMode(relayPin, OUTPUT);
  digitalWrite(relayPin, LOW);

  digitalWrite(TFT_CS, HIGH);
  pinMode(TFT_CS, OUTPUT);

  Serial.begin(115200);  // USB serial monitor
  Serial1.begin(115200); // UART: D1 TX, D0 RX

  stateStartMillis = millis();

  if (flashTime < ramp1Time * 1000) {
    currentState = RAMP1;
    stateStartMillis = 0;
  }
  else if (flashTime < (ramp1Time + hold1Time) * 1000) {
    currentState = HOLD1;
    stateStartMillis = ramp1Time * 1000;
  }
  else if (flashTime < (ramp1Time + hold1Time + ramp2Time) * 1000) {
    currentState = RAMP2;
    stateStartMillis = (ramp1Time + hold1Time) * 1000;
  }
  else if (flashTime < totTime * 1000) {
    currentState = HOLD2;
    stateStartMillis = (ramp1Time + hold1Time + ramp2Time) * 1000;
  }
  else {
    currentState = COMPLETE;
    stateStartMillis = totTime * 1000;
  }

  roomTemp = readTemperature();

  tft.init(240, 320);
  tft.setRotation(3);
  tft.fillScreen(ST77XX_BLACK);

  tft.drawLine(x_1, y_1, x_2, y_2, ST77XX_WHITE);
  tft.drawLine(x_2, y_2, x_3, y_2, ST77XX_WHITE);
  tft.drawLine(x_3, y_2, x_4, y_3, ST77XX_WHITE);
  tft.drawLine(x_4, y_3, x_5, y_3, ST77XX_WHITE);
}

void loop() {
  currentTemperature = readTemperature();
  elapsedMillis = millis() + flashTime;

  Serial.print("flashTime: ");
  Serial.println(flashTime);

  Serial.print("ramp1Time: ");
  Serial.println(ramp1Time * 1000);

  Serial.print("hold1Time: ");
  Serial.println(hold1Time * 1000);

  Serial.print("ramp2Time: ");
  Serial.println(ramp2Time * 1000);

  Serial.print("hold2Time: ");
  Serial.println(hold2Time * 1000);

  Serial.print("totTime: ");
  Serial.println(totTime * 1000);

  updateState();

  float setPoint = 0;

  switch (currentState) {
    case RAMP1:
      setPoint = calculateSetPoint(
        stateMillis,
        roomTemp,
        hold1Temp,
        ramp1Time
      );
      break;

    case HOLD1:
      setPoint = hold1Temp;
      break;

    case RAMP2:
      setPoint = calculateSetPoint(
        stateMillis,
        hold1Temp,
        hold2Temp,
        ramp2Time
      );
      break;

    case HOLD2:
      setPoint = hold2Temp;
      break;

    case COMPLETE:
      digitalWrite(relayPin, LOW);
      setPoint = 0;
      break;
  }

  if (currentState != COMPLETE) {
    controlHeating(setPoint);
  }

  // Send live readings through pins D1 (TX) and D0 (RX).
  printUARTStatus(setPoint);

  // Update display.
  writeScreen(
    currentTemperature,
    elapsedMillis / 1000,
    setPoint
  );

  delay(1000);
}