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

## 1. Install Python (once)

Install Python 3.10 or newer from <https://www.python.org/downloads/>.

- **Windows:** tick **"Add python.exe to PATH"** in the installer.
- **macOS:** the python.org installer includes tkinter (the GUI toolkit). If you use
  Homebrew's Python instead, also run `brew install python-tk`.

## 2. Run the GUI from source

Setup creates a private `.venv` folder and installs everything into it, so nothing
touches your system Python.

| | Windows | macOS / Linux |
|---|---|---|
| Install dependencies | double-click `setup_windows.bat` | `bash setup_mac.sh` |
| Start the GUI | double-click `run_windows.bat` | `bash run_mac.sh` |
| Try it without hardware | `run_windows.bat --sim` | `bash run_mac.sh --sim` |

The run scripts call setup automatically the first time, so you can also skip
straight to them.

## 3. Build a standalone app

The standalone app bundles Python and all dependencies, so it runs on computers
with nothing installed. PyInstaller builds for the system it runs on, so **build the
.exe on Windows and the .app on a Mac**.

| | Command | Output |
|---|---|---|
| Windows | double-click `build_windows.bat` | `dist\CureOven.exe` (one file) |
| macOS | `bash build_mac.sh` | `dist/CureOven.app` and `dist/CureOven-macOS.zip` |

A Mac build targets the kind of Mac it was built on (Apple Silicon or Intel).

**Building both without owning both:** push this folder to a GitHub repository, open
**Actions → Build apps → Run workflow**, and download `CureOven-Windows` and
`CureOven-macOS-arm64` from the finished run's Artifacts section.

### First launch of the built app

The apps aren't code-signed, so the OS warns the first time:

- **Windows SmartScreen:** click **More info → Run anyway**.
- **macOS Gatekeeper:** right-click `CureOven.app` → **Open** → **Open**. If macOS says
  the app is damaged (common after downloading the zip), run once in Terminal:
  `xattr -dr com.apple.quarantine /path/to/CureOven.app`

## 4. Hardware connection

The firmware talks on **Serial1: pins D0 (RX) and D1 (TX), 115200 baud**. Nothing is
sent over the R4's USB-C port, which is only used for uploading.

Use a **5 V USB-to-TTL adapter**:

| Adapter | Uno R4 |
|---|---|
| TX | D0 (RX) |
| RX | D1 (TX) |
| GND | GND |

In the GUI, pick the adapter's port (e.g. `COM5` on Windows,
`/dev/cu.usbserial-XXXX` on macOS) and click **Connect**.

Adapters with a CH340 chip may need a driver on Windows and older macOS versions;
FTDI and CP210x adapters usually work out of the box.

## 5. Logs

Every session is saved as a CSV in **Documents/CureOvenLogs** (use the **Open logs**
button). Change the folder with `--log-dir <path>`.

## Troubleshooting

- **"Could not open port":** another program (Arduino Serial Monitor, another GUI
  window) is holding it. Close that and reconnect.
- **Port doesn't appear:** unplug and replug the adapter, click **Refresh ports**,
  and check the adapter driver.
- **Connected but no data:** TX and RX are probably not crossed, or GND isn't shared.
- **`No module named tkinter` on macOS/Linux:** see step 1 (`brew install python-tk`,
  or `sudo apt install python3-tk python3-venv` on Ubuntu).
