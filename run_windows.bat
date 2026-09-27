@echo off
rem Starts the GUI. Extra arguments are passed through, e.g. run_windows.bat --sim
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe call setup_windows.bat
.venv\Scripts\python.exe app\cure_gui.py %*
if errorlevel 1 pause
