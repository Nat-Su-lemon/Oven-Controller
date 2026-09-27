@echo off
rem Builds dist\CureOven.exe (a single file you can copy to any Windows PC).
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe call setup_windows.bat
call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt -r requirements-build.txt || goto :fail
pyinstaller --noconfirm --clean CureOven.spec || goto :fail
echo.
echo Built: %cd%\dist\CureOven.exe
pause
exit /b 0
:fail
echo Build failed, see the messages above.
pause
exit /b 1
