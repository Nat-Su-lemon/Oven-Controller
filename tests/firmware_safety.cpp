// Execute the actual sketch with fake ADC, relay, clock, EEPROM and display.
#include "../firmware/cure_controller/cure_controller.ino"
#include <cassert>
#include <iostream>

uint32_t fakeMillis = 0;
int fakeAdc = 198;
bool relayOutput = false;
FakeSerial Serial1;
FakeEeprom EEPROM;

static void command(const char* text) {
  char line[80];
  std::strcpy(line, text);
  handleCommand(line);
}

static void fresh() {
  fakeMillis = 0;
  fakeAdc = 198;
  EEPROM = FakeEeprom();
  Serial1.output.str("");
  P = DEFAULT_PARAMS;
  running = paused = faulted = false;
  faultCode = FAULT_NONE;
  sensorBadCount = 0;
  phase = IDLE;
  cycleMs = 0;
  heatWatch.reset(0);
  setup();
}

static void seconds(unsigned int n) {
  while (n--) { fakeMillis += 1000; loop(); }
}

static void temperatureCurve() {
  fresh();
  // Datasheet curve 8307, independent nominal R/T values (not beta-generated).
  const float temps[] = {0, 25, 65, 120, 180, 200, 250};
  const float resistances[] = {28024, 10000, 2539.2f, 581.44f, 169.61f, 119.99f, 56.316f};
  for (unsigned int i = 0; i < 7; ++i) {
    fakeAdc = (int)std::round(1023 * R2 / (R2 + resistances[i]));
    // The original 2402 ohm divider has coarser resolution near 250 C.
    assert(std::fabs(readTemperature() - temps[i]) < (temps[i] > 200 ? 2.0f : 1.1f));
  }
  fakeAdc = 0; assert(std::isnan(readTemperature()));
  fakeAdc = 1023; assert(std::isnan(readTemperature()));
}

static void stuckSensorAndPersistentLatch() {
  fresh();
  assert(startRun(38)); // HOLD1 at 120 C, sensor fixed near 25 C
  seconds(1);
  assert(heaterOn && relayOutput);
  seconds(90);
  command("CLEAR"); // no fault: must not restart the safety window
  seconds(89);
  assert(!faulted);
  seconds(1);
  assert(faulted && faultCode == FAULT_NO_HEAT && !heaterOn && !relayOutput);
  unsigned long frozen = cycleMs;
  seconds(5);
  assert(cycleMs == frozen);
  assert(!startRun(0));
  command("GOTO 40");
  assert(faulted && cycleMs == frozen);
  command("ABORT");
  command("DEFAULTS");
  assert(faulted);
  // Simulate a power cycle: RAM latch is gone, saved header must restore it.
  faulted = false;
  faultCode = FAULT_NONE;
  setup();
  assert(faulted && faultCode == FAULT_NO_HEAT && !relayOutput && !running);
  printData();
  assert(Serial1.output.str().find("reason=no_heat") != std::string::npos);
  command("CLEAR");
  assert(!faulted && faultCode == FAULT_NONE);
  assert(startRun(0));
}

static void invalidSensorAndOvertemp() {
  fresh();
  assert(startRun(38));
  seconds(1);
  fakeAdc = 0;
  seconds(1);
  assert(!heaterOn && !relayOutput && !faulted);
  seconds(2);
  assert(faulted && faultCode == FAULT_SENSOR);
  command("CLEAR");
  assert(faulted);
  fakeAdc = 198;
  command("CLEAR");
  assert(!faulted);
  fakeAdc = 990; // about 230 C: valid sensor, above maxTemp
  seconds(1);
  assert(faulted && faultCode == FAULT_OVERTEMP && !relayOutput);
  command("CLEAR");
  assert(faulted);
  fakeAdc = 198;
  command("CLEAR");
  assert(!faulted);
  float original = P.maxTemp;
  command("SET maxtemp nan"); command("SET maxtemp inf");
  assert(P.maxTemp == original);
}

static void legacyAndRunningFaultRestore() {
  fresh();
  assert(startRun(38));
  // The legacy header used this same byte as 0 or 1: preserve resumability.
  Header legacy;
  EEPROM.get(HEADER_ADDR, legacy);
  assert(legacy.runFlags == 1);
  running = false;
  assert(loadFromEeprom() && running && !faulted);
  raiseFault(FAULT_NO_HEAT);
  running = faulted = false;
  faultCode = FAULT_NONE;
  setup();
  assert(running && faulted && faultCode == FAULT_NO_HEAT && !relayOutput);
  unsigned long frozen = cycleMs;
  seconds(10);
  assert(cycleMs == frozen && !relayOutput);
}

static void watchdogEdges() {
  HeatingWatchdog watch(180000, 2);
  watch.reset(0);
  assert(!watch.update(0, true, 25, false));
  assert(!watch.update(100000, true, 27, false)); // adequate rise renews window
  assert(!watch.update(279999, true, 27, false));
  assert(watch.update(280000, true, 27, false));
  watch.reset(0);
  watch.update(0, true, 25, false);
  watch.update(90000, false, NAN, false); // transient bad read must retain time
  watch.update(100000, true, 25, false);
  assert(watch.update(190000, true, 25, false));
  watch.reset(0);
  watch.update(0, true, 25, false);
  watch.update(179000, false, 120, true); // normal thermostat off resets
  assert(!watch.update(500000, false, 119, true));
  watch.reset(0xffff0000U);
  watch.update(0xffff0000U, true, 25, false);
  assert(watch.update((uint32_t)(0xffff0000U + 180000U), true, 25, false));
  // Low oscillation never lowers the baseline to manufacture a new rise.
  watch.reset(0);
  watch.update(0, true, 25, false);
  watch.update(90000, true, 22, false);
  assert(watch.update(180000, true, 25, false));
}

int main() {
  temperatureCurve();
  stuckSensorAndPersistentLatch();
  invalidSensorAndOvertemp();
  legacyAndRunningFaultRestore();
  watchdogEdges();
  fresh();
  assert(startRun(38));
  command("PAUSE");
  seconds(181);
  assert(faulted && faultCode == FAULT_NO_HEAT); // PAUSE still heats
  std::cout << "Firmware safety checks passed\n";
}
