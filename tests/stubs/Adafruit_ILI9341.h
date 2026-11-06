#pragma once
#include "Arduino.h"
constexpr int ILI9341_BLACK = 0, ILI9341_WHITE = 1, ILI9341_RED = 2, ILI9341_YELLOW = 3;
class Adafruit_ILI9341 {
 public:
  Adafruit_ILI9341(int, int, int) {}
  void begin() {}
  void setRotation(int) {}
  void fillScreen(int) {}
  void setTextColor(int) {}
  void setTextSize(int) {}
  void setCursor(int, int) {}
  void drawLine(int, int, int, int, int) {}
  void drawPixel(int, int, int) {}
  void fillRect(int, int, int, int, int) {}
  template <typename T> void print(T) {}
};
