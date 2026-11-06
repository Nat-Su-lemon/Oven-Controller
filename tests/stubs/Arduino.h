#pragma once
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <sstream>

#ifdef _MSC_VER
#define strcasecmp _stricmp
#endif
class __FlashStringHelper;
#define F(text) reinterpret_cast<const __FlashStringHelper*>(text)
constexpr int A2 = 2, HIGH = 1, LOW = 0, INPUT = 0, OUTPUT = 1, AR_DEFAULT = 0;
extern uint32_t fakeMillis;
extern int fakeAdc;
extern bool relayOutput;
inline unsigned long millis() { return fakeMillis; }
inline int analogRead(int) { return fakeAdc; }
inline void analogReadResolution(int) {}
inline void analogReference(int) {}
inline void pinMode(int, int) {}
inline void digitalWrite(int pin, int value) { if (pin == 2) relayOutput = value == HIGH; }
template <typename T> T constrain(T v, T low, T high) { return std::min(high, std::max(low, v)); }
class FakeSerial {
 public:
  std::ostringstream output;
  void begin(int) {}
  int available() { return 0; }
  char read() { return 0; }
  void print(const __FlashStringHelper* s) { output << reinterpret_cast<const char*>(s); }
  template <typename T> void print(T v) { output << v; }
  template <typename T> void print(T v, int) { print(v); }
  template <typename T> void println(T v) { print(v); output << '\n'; }
  template <typename T> void println(T v, int) { println(v); }
};
extern FakeSerial Serial1;
