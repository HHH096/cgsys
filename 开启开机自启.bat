@echo off
rem Keep this file ASCII-only and do NOT add "chcp" (see the design doc,
rem section 10: cmd.exe loses its read position when the page changes).
setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] No runtime environment found.
    echo         Run the launcher once first, then try again.
    pause
    exit /b 1
)

"%PY%" -m app.tools.autostart on
echo.
echo Note: set this account to log in automatically, otherwise the server
echo       will not come back up after a reboot.
echo.
pause
