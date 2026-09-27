@echo off
rem Creates .venv and installs the GUI's dependencies.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")
%PY% --version >nul 2>nul
if errorlevel 1 (
    echo Python 3 was not found. Install it from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during install.
    pause
    exit /b 1
)
if not exist .venv (
    echo Creating virtual environment...
    %PY% -m venv .venv || goto :fail
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip || goto :fail
python -m pip install -r requirements.txt || goto :fail
echo.
echo Setup done. Double-click run_windows.bat to start the GUI.
pause
exit /b 0
:fail
echo Setup failed, see the messages above.
pause
exit /b 1
