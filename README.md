# Cure Oven Controller

Arduino Uno R4 cure-cycle controller (Toray 3960 profile) plus a desktop GUI that
monitors and controls it over serial.

```
cure-oven/
├── app/cure_gui.py                  the GUI (single Python file)
├── firmware/cure_controller/        Arduino sketch, open this folder in the Arduino IDE
├── requirements.txt                 GUI dependencies (pyserial, matplotlib)
├── requirements-build.txt           extra dependency for building the app (pyinstaller)
├── CureOven.spec                    PyInstaller recipe for the .exe / .app
├── setup_windows.bat  run_windows.bat  build_windows.bat
├── setup_mac.sh       run_mac.sh       build_mac.sh      (also work on Linux)
└── .github/workflows/build.yml      optional: build both apps on GitHub
```

---

## Quick start

### Firmware

1. Open `firmware/cure_controller/cure_controller.ino` in the Arduino IDE.
2. Select the **Arduino Uno R4** board you have (Minima or WiFi).
3. In Library Manager, install **Adafruit ILI9341** and accept its dependencies
   (Adafruit GFX, Adafruit BusIO).
4. Upload.

### GUI from source

Install Python 3.10 or newer from <https://www.python.org/downloads/> (on Windows,
tick **"Add python.exe to PATH"**; on macOS with Homebrew Python, also run
`brew install python-tk`).

| | Windows | macOS / Linux |
|---|---|---|
| Install dependencies | double-click `setup_windows.bat` | `bash setup_mac.sh` |
| Start the GUI | double-click `run_windows.bat` | `bash run_mac.sh` |
| Try it without hardware | `run_windows.bat --sim` | `bash run_mac.sh --sim` |

Setup creates a private `.venv` folder and installs everything into it. The run
scripts call setup automatically the first time.

### Standalone app (.exe / .app)

PyInstaller builds for the system it runs on: build the .exe on Windows and the .app
on a Mac (the Mac build targets that Mac's chip, Apple Silicon or Intel).

| | Command | Output |
|---|---|---|
| Windows | double-click `build_windows.bat` | `dist\CureOven.exe` (one file) |
| macOS | `bash build_mac.sh` | `dist/CureOven.app` and `dist/CureOven-macOS.zip` |

To build both without owning both machines, push this folder to GitHub (contents at
the repo root, so `.github/` is at the top level), then open **Actions → Build apps →
Run workflow** and download `CureOven-Windows` and `CureOven-macOS-arm64` from the
run's Artifacts section.

The apps are not code-signed, so the first launch shows a warning:

- **Windows SmartScreen:** More info → Run anyway.
- **macOS:** right-click `CureOven.app` → Open → Open. If macOS says the app is
  damaged, run `xattr -dr com.apple.quarantine /path/to/CureOven.app` once.

---

## Hardware connection

| Item | Pin |
|---|---|
| Thermistor divider output | A0 |
| Heater relay | D2 (HIGH = heater on) |
| TFT (ILI9341, 240x320, SPI) | CS D10, DC D9, RST D8, MOSI D11, SCK D13, MISO D12 (optional), plus VCC, GND and LED/backlight |
| Serial link to computer | **Serial1: D0 (RX), D1 (TX), 115200 baud** |

**Screen logic level:** the Uno R4's pins are 5 V. Adafruit's ILI9341 breakouts
have level shifting built in, but most generic ILI9341 modules (the red 2.4" to 3.2"
boards) are **3.3 V logic** and need a level shifter (or series resistors) on CS, DC,
RST, MOSI and SCK. If the image is upside down, change `tft.setRotation(3)` to `1`.

Nothing is sent over the R4's USB-C port; it is only used for uploading. Connect the
computer through a **5 V USB-to-TTL adapter**:

| Adapter | Uno R4 |
|---|---|
| TX | D0 (RX) |
| RX | D1 (TX) |
| GND | GND |

---

## How the controller works

### Cure profile

The cure is four phases followed by COMPLETE. The setpoint at any moment is computed
from the **cycle clock** (time into the cure):

| Phase | Setpoint | Default |
|---|---|---|
| RAMP1 | straight line from the **start temperature** to hold 1 temp | 38 min, start → 120 °C |
| HOLD1 | hold 1 temp | 120 °C for 240 min |
| RAMP2 | straight line from hold 1 temp to hold 2 temp | 24 min, 120 → 180 °C |
| HOLD2 | hold 2 temp | 180 °C for 120 min |
| COMPLETE | none, heater off | |

Default total: 422 min (7 h 2 min). The **start temperature** is the oven
temperature measured at the moment the cure is started, so ramp 1 always begins from
wherever the oven actually is.

Before a cure is started the controller is **IDLE** (heater off).

### Heater control

Once per second the board reads the temperature and runs an on/off controller with
hysteresis (same logic as the original code):

- heater turns **on** when temperature < setpoint - hysteresis
- heater turns **off** when temperature > setpoint + hysteresis
- in between, it stays as it was

The heater is forced **off** whenever: no cure is running (IDLE, aborted, complete),
a fault is latched, or the current temperature reading is invalid (even before a
sensor fault has latched).

### Temperature measurement

Each reading averages **8 ADC samples** on A0, then converts with the original
divider and Steinhart-Hart constants:

```
V = 5.0 * adc / 1024
R = R2 * (Vcc - V) / V          R2 = 2402 ohm, Vcc = 5.061 V
1/T = A + B ln(R) + C ln(R)^3   A = 4.5421e-4, B = 3.5529e-4, C = -2.7612e-7
```

**Calibration note (unchanged from the original, worth checking):** the ADC counts
are converted with 5.0 V while the divider math uses Vcc = 5.061 V. If the ADC
reference is the 5 V rail (the R4 default), this mismatch makes the reading roughly
10 to 12 °C low near 180 °C, unless A, B and C were fitted using this same formula.
Verify against a thermocouple at the 180 °C hold before trusting the numbers.

### Cycle clock

The cycle clock advances only while a cure is running and is **not paused and not
faulted**. Pausing or faulting freezes the cure at that point in the profile; the
remaining time is added on when it continues.

### Saving and resuming (EEPROM)

The board stores its state in EEPROM (flash-emulated on the R4):

- **Parameters, start temperature, and whether a cure is running** are saved whenever
  they change (START, GOTO, SET, DEFAULTS, ABORT, COMPLETE).
- **Cycle clock** is saved every 30 s and on PAUSE, rotated across 32 slots to spread
  flash wear.

At power-up:

- If a cure was running, the board **resumes it automatically** from the last saved
  clock position (up to 30 s earlier than where it stopped) and prints
  `EVENT resumed t=...`.
- Otherwise it waits in IDLE for a START command (see `AUTO_START` below).

### Vacuum

During the cure and after COMPLETE the GUI shows **Keep vacuum on**. After COMPLETE,
once the temperature is at or below **65 °C**, the board sends
`EVENT vacuum_safe` once, `DATA` shows `vac=1`, and the GUI shows that it is safe to
release vacuum.

---

## Faults

A fault **latches**: the heater turns off, the cycle clock freezes, the TFT shows
**FAULT** in red, the board prints `EVENT fault reason=... temp=...`, every `DATA`
line has `fault=1`, and the GUI shows a red banner. It stays latched until you send
**CLEAR** (GUI: Clear fault).

| Fault | Trigger | Typical causes |
|---|---|---|
| `sensor` | Temperature reading invalid for **3 readings in a row** (about 3 s). Invalid means: ADC reads about 0 (open circuit), the math gives NaN or infinity, or the result is below **-20 °C** or above **300 °C**. The heater is already off from the first bad reading. | Thermistor unplugged or broken wire, shorted thermistor, loose A0 connection, missing R2 |
| `overtemp` | A valid reading above the **over-temp cutoff** (`maxtemp`, default **200 °C**). Immediate, no delay. | Relay stuck or slow to release, hysteresis too large, cutoff set too close to hold 2 temp, oven overshoot |

**Clearing a fault:**

1. Fix the cause.
2. Send CLEAR. The cure continues from exactly where the clock froze.
3. If the cause is still present, it faults again: overtemp within 1 s, sensor after
   3 bad readings.

Because the clock was frozen, a fault during a hold does **not** eat into hold time,
but the part cools while the heater is off. Decide whether the cure is still valid
before clearing.

**Other ways a fault is cleared** (be aware of these):

- `START` and `GOTO` also clear a fault (and a pause) when they run.
- A board **reset or power cycle clears the latch** (the fault itself is not saved to
  EEPROM). If a cure was running, it resumes; if the cause is still present it will
  fault again within a few seconds.

**What the software cannot detect** (use independent hardware protection):

- **Thermistor displaced but still connected** (fell off the part, reading room air):
  it reads low and valid, so the heater stays on. There is no "heater on but
  temperature not rising" check.
- **Relay welded shut:** software turns the pin off but the heater stays powered.
  Overtemp will latch, but cannot cut power.
- **Board freeze:** there is no watchdog; a hung board leaves the relay in its last
  state.

A thermal fuse or snap-disc thermostat in series with the heater, rated just above
the highest cure temperature, covers all three.

---

## Serial protocol

115200 baud on Serial1, lines end with a newline. Every line from the board starts
with a tag.

### From the board

| Tag | When | Example |
|---|---|---|
| `DATA` | every second, and on STATUS | `DATA t=61.0 state=RAMP1 paused=0 fault=0 temp=25.10 set=26.44 err=1.34 heater=1 st=61 srem=2219 rem=25259 vac=0 adc=301.5 R=5876.2 up=64` |
| `PARAMS` | at boot, on PARAMS, after SET, DEFAULTS, START, GOTO | `PARAMS ramp1=38.00 hold1temp=120.00 hold1=240.00 ramp2=24.00 hold2temp=180.00 hold2=120.00 hyst=2.00 maxtemp=200.00 start=23.41 total=422.00` |
| `EVENT` | something happened | `EVENT state from=RAMP1 to=HOLD1 t=2280.0` |
| `OK` / `ERR` | reply to a command | `OK set hold1=250.00`, `ERR not running` |
| `INFO` | boot messages, help, warnings | `INFO idle, send START to begin a cure` |

`DATA` fields:

| Field | Meaning |
|---|---|
| `t` | cycle clock, seconds |
| `state` | IDLE, RAMP1, HOLD1, RAMP2, HOLD2, COMPLETE |
| `paused`, `fault` | 1 if paused / fault latched |
| `temp` | temperature, °C (`nan` if invalid) |
| `set` | setpoint, °C (0 when not running) |
| `err` | setpoint minus temperature (positive = oven below setpoint) |
| `heater` | 1 if relay is on |
| `st`, `srem` | seconds into the current phase, seconds left in it |
| `rem` | seconds left in the whole cure |
| `vac` | 1 once COMPLETE and at or below 65 °C |
| `adc` | averaged raw ADC value (0 to 1023) |
| `R` | computed thermistor resistance, ohms |
| `up` | seconds since the board powered up |

Events: `start`, `state`, `pause`, `resume`, `abort`, `complete`, `fault`,
`fault_cleared`, `vacuum_safe`, `resumed`.

### Commands to the board

Case-insensitive. Also usable from any serial terminal on the adapter's port
(line ending "Newline").

| Command | Effect |
|---|---|
| `START` | Start a new cure at t = 0. Measures the start temperature; if the thermistor reading is invalid it replies `ERR cannot start...` and changes nothing. Clears pause and fault. |
| `GOTO <min>` | Jump to that minute of the cycle (0 to total length). If no cure is running, it starts one there and measures the start temperature. Clears pause and fault. Replaces the old `flashTime`. |
| `PAUSE` | Freeze the cycle clock. The heater **keeps holding the current setpoint** (during a ramp, that is the temperature reached so far). |
| `RESUME` | Unfreeze the clock. |
| `ABORT` | Heater off, cure ends, back to IDLE. Cannot be resumed; use START or GOTO. |
| `CLEAR` | Clear a latched fault. |
| `SET <name> <value>` | Change a parameter (below). Saved to EEPROM, takes effect **immediately, even mid-cure**. |
| `DEFAULTS` | Restore the Toray 3960 defaults and save them. |
| `PARAMS` | Print parameters. |
| `STATUS` | Print one DATA line now. |
| `HELP` | List commands. |

Parameters:

| Name | Meaning | Default | Allowed |
|---|---|---|---|
| `ramp1` | ramp 1 time, min | 38 | 0 to 1440 |
| `hold1temp` | hold 1 temperature, °C | 120 | 0 to 250 |
| `hold1` | hold 1 time, min | 240 | 0 to 1440 |
| `ramp2` | ramp 2 time, min | 24 | 0 to 1440 |
| `hold2temp` | hold 2 temperature, °C | 180 | 0 to 250 |
| `hold2` | hold 2 time, min | 120 | 0 to 1440 |
| `hyst` | hysteresis, ± °C | 2 | 0.1 to 20 |
| `maxtemp` | over-temp cutoff, °C | 200 | 50 to 280 |

Things to know about changing parameters:

- **Mid-cure changes move where you are in the profile.** The phase is computed from
  the cycle clock. If you shorten the current phase (or an earlier one) so the cycle
  clock is now past its end, the cure skips ahead to the matching phase immediately.
  Lengthening a phase extends it.
- **Parameters live in EEPROM.** Editing `DEFAULT_PARAMS` in the sketch and
  re-uploading does **not** change a board that already has saved parameters. Send
  `DEFAULTS` (or SET) after uploading. Defaults are only loaded automatically on a
  board whose EEPROM has never held this sketch's data.
- The board warns if `maxtemp` is at or below `hold2temp`, since the cure would fault.

---

## The GUI

- **Plot:** measured temperature, setpoint and the planned profile against cycle
  time, with a heater on/off strip underneath and a dotted line at 65 °C. After
  COMPLETE the plot keeps extending (using board uptime) so the cool-down is visible.
- **Now panel:** temperature, setpoint, heater, state (with paused/FAULT), cycle
  time, time left in the phase and in total, raw ADC and resistance, vacuum status,
  and a red fault banner with a hint about the cause.
- **Run buttons:** Start cure (asks before restarting a running cure, and offers to
  send unsent parameter edits first), Pause, Resume, Abort (asks to confirm), Clear
  fault, Status, and Jump to minute (sends GOTO).
- **Cure parameters:** edited fields turn yellow until sent. **Send** transmits only
  the changed fields (asks to confirm if a cure is running). **Reload** reads them back
  from the board; **Defaults** restores the Toray defaults. The hint underneath shows
  the resulting ramp rates and total time, and warns when the cutoff is too close to
  hold 2.
- **Serial console:** every line from the board (DATA lines hidden unless "Show DATA
  lines" is ticked), plus a box to type any command. Up/Down recalls past commands.
- **Logging:** every session is saved as a CSV in **Documents/CureOvenLogs** (Open
  logs button; change with `--log-dir <path>`). Each row has the wall-clock time and
  all DATA fields; non-DATA lines and commands you sent are logged in the `note`
  column.
- **Simulator:** choose "Simulator (no hardware)" in the port list or start with
  `--sim`. It imitates the firmware's commands and output at 60x speed with a simple
  oven model, for trying the GUI safely.

### Choosing the port

The port list shows every serial port with its driver description. The GUI
pre-selects the first one whose name contains "arduino", "ch340" or "usb", which
may be the **R4's own USB-C port** rather than the adapter. It does not check the
data before you connect. To find the adapter: unplug it, click Refresh ports, and
see which entry disappears. Adapters usually appear as `CH340`, `FT232R USB UART`,
`CP210x`, or `/dev/cu.usbserial-…` on macOS. When the right port is connected you
see INFO/PARAMS/DATA lines within a second or two.

---

## Differences from the original sketch

### Functional changes

| Original | Now |
|---|---|
| Cure started immediately on every power-up or reset. | Waits in IDLE for **START** on a fresh power-up. Set `#define AUTO_START 1` to start automatically (if no cure was in progress). |
| A reset or power blip restarted the cure from `flashTime`. | **Resumes automatically** from EEPROM, up to 30 s behind where it stopped. Time spent powered off is not counted. |
| Jumping into the cycle required editing `flashTime` and re-uploading. | `GOTO <min>` over serial or the GUI's Jump button. `flashTime` is gone. |
| Parameters were constants in the code. | Changeable at runtime with `SET` or the GUI, **saved in EEPROM**, and survive re-uploads (see "Parameters live in EEPROM"). |
| No pause or abort; the only way to stop was cutting power. | `PAUSE` / `RESUME` (clock frozen, setpoint held) and `ABORT` (heater off, back to IDLE). |
| Ramp 1 started from the temperature read at boot. | Ramp 1 starts from the temperature at **START** (or at GOTO when no cure was running), saved in EEPROM for resumes. |
| Clock (`millis() + flashTime`) kept running no matter what. | Clock stops while paused or faulted, so holds are not shortened by interruptions. |
| COMPLETE turned the heater off; no notice for vacuum release. | Same heater behavior, plus a one-time `vacuum_safe` event and GUI message at or below 65 °C. (After ABORT there is no vacuum notice; watch the temperature.) |

### Safety changes

| Original | Now |
|---|---|
| No fault detection. An unplugged thermistor produced NaN; NaN fails both comparisons in `controlHeating`, so a relay that was on **stayed on indefinitely**. | Invalid readings turn the heater off immediately and latch a `sensor` fault after 3 in a row. |
| No over-temperature limit. | `overtemp` fault above `maxtemp` (default 200 °C). |
| Heater state read back with `digitalRead` on the output pin. | Heater state tracked in a variable and set in one place. |

### Timing and accuracy changes

| Original | Now |
|---|---|
| Phases advanced by a state machine checked once per second, restarting each phase's timer at the moment the change was noticed, so each phase could run up to about 1 s long. | Phase and setpoint computed directly from the cycle clock; phase lengths are exact. |
| On the first tick of ramp 2, the time already spent in hold 1 was used as ramp progress, so the setpoint jumped to 180 °C for one second and could switch the heater on. | Fixed. |
| `delay(1000)` blocked the whole loop. | Non-blocking: control runs every 1000 ms, serial commands are handled continuously. |
| Single ADC read per measurement. | Average of 8 ADC reads. |
| `writeScreen` was declared `float` but returned nothing (undefined behavior in C++). | Fixed. |

### Serial changes

| Original | Now |
|---|---|
| Printed `flashTime` and the phase lengths (constants) on USB `Serial` every second. | Structured `DATA`, `PARAMS`, `EVENT`, `OK`/`ERR`, `INFO` lines on **Serial1 (D0/D1)**. Nothing on USB. |
| No serial input. | Full command set (see Serial protocol). |

### Display changes

| Original | Now |
|---|---|
| Profile drawn with temperature scaled from 0 °C, but the live trace scaled as (temp - room temp) / 180, so the red trace sat below the white profile even when on target. | Profile and trace share one scale (20 °C to hold 2 temp), and points are clamped to the screen. |
| Title redrawn every second. | Title drawn with the profile; screen redrawn on START, GOTO and parameter changes (this clears the red trace). |
| State field showed the phase name. | Also shows **FAULT** (red) and **PAUSED** (yellow); temperature shows **ERR** when the reading is invalid. |
| Red trace drawn always. | Drawn only while a cure is running and the reading is valid. |
| ST7789 display (`Adafruit_ST7789`, `tft.init(240, 320)`, `ST77XX_` colors). | ILI9341 display (`Adafruit_ILI9341`, `tft.begin()`, `ILI9341_` colors). Same 320x240 landscape layout and pins. |

### Unchanged

Pins, screen resolution and rotation, thermistor constants and conversion formula (including
the 5.0 V vs 5.061 V note above), default Toray 3960 profile values, hysteresis
control logic, 1 s control period, and 115200 baud.

---

## Troubleshooting

- **"Could not open port":** another program (Arduino Serial Monitor, another GUI
  window) is holding it. Close it and reconnect.
- **Port doesn't appear:** replug the adapter, click Refresh ports, check the driver
  (CH340 adapters may need one on Windows and older macOS).
- **Connected but no data:** TX/RX not crossed, GND not shared, or the R4's USB-C port
  was selected instead of the adapter.
- **START answers `ERR cannot start: thermistor reading invalid`:** check the
  thermistor wiring; the `adc` and `R` values in STATUS help.
- **Uploaded new defaults but the board still uses old values:** send `DEFAULTS`.
- **`No module named tkinter`:** `brew install python-tk` (Homebrew) or
  `sudo apt install python3-tk python3-venv` (Ubuntu).
