#pragma once
#include <array>
#include <cstring>
class FakeEeprom {
 public:
  std::array<unsigned char, 1024> bytes;
  int writes = 0;
  FakeEeprom() { bytes.fill(0xff); }
  template <typename T> void put(int address, const T& value) {
    std::memcpy(bytes.data() + address, &value, sizeof(T));
    ++writes;
  }
  template <typename T> void get(int address, T& value) {
    std::memcpy(&value, bytes.data() + address, sizeof(T));
  }
};
extern FakeEeprom EEPROM;
