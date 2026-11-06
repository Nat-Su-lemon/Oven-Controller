# Safety regression checks

From the repository root, using the GUI's Python environment:

```sh
python -m unittest discover -s tests -p "test_*.py" -v
```

The C++ harness executes the actual sketch with fake Arduino peripherals. On
Windows, use a Visual Studio Developer Command Prompt, create `build` if needed,
then run:

```bat
cl /nologo /std:c++14 /EHsc /Itests/stubs tests/firmware_safety.cpp /Febuild/firmware_safety.exe /Fobuild/firmware_safety.obj
build\firmware_safety.exe
```

On a host with GCC:

```sh
g++ -std=c++14 -Itests/stubs tests/firmware_safety.cpp -o build/firmware_safety
./build/firmware_safety
```

These checks cover nominal thermistor table points, ADC rails, stuck-low readings,
invalid-sample shutdown, over-temperature, clock freezing, fault persistence,
legacy EEPROM compatibility, explicit CLEAR, paused heating, watchdog renewal,
intermittent faults, timer wrap, GUI reconnection/logging, parameter validation,
and a complete simulated cure. They do not measure the physical oven's response;
validate the 2 °C / 180 s thresholds and temperature calibration on the real load.
