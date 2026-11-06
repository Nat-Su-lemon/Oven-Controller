#pragma once

#include <math.h>
#include <stdint.h>

// Count commanded heater-on time between measurable temperature rises. Brief
// invalid samples suspend heating without discarding the accumulated exposure.
class HeatingWatchdog {
 public:
  HeatingWatchdog(uint32_t timeoutMs, float minRiseC)
      : timeoutMs_(timeoutMs), minRiseC_(minRiseC) {}

  void reset(uint32_t now) {
    active_ = wasHeating_ = false;
    onMs_ = 0;
    lastMs_ = now;
  }

  bool update(uint32_t now, bool heating, float temperature, bool satisfied) {
    uint32_t dt = now - lastMs_;  // unsigned subtraction handles millis wrap
    lastMs_ = now;
    if (active_ && wasHeating_) onMs_ += dt;
    wasHeating_ = heating;

    if (satisfied) {
      reset(now);
      return false;
    }
    if (!active_) {
      if (heating && isfinite(temperature)) {
        active_ = true;
        baselineC_ = temperature;
        onMs_ = 0;
      }
      return false;
    }
    if (isfinite(temperature) && temperature >= baselineC_ + minRiseC_) {
      baselineC_ = temperature;
      onMs_ = 0;
    }
    return onMs_ >= timeoutMs_;
  }

 private:
  const uint32_t timeoutMs_;
  const float minRiseC_;
  bool active_ = false;
  bool wasHeating_ = false;
  uint32_t lastMs_ = 0;
  uint32_t onMs_ = 0;
  float baselineC_ = 0;
};
