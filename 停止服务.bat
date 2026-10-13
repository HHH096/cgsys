@echo off
rem Stop the background server (there is no window to close).
rem Keep this file ASCII-only and do NOT add "chcp" (see the design doc, section 10).
setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] No runtime environment found.
    pause
    exit /b 1
)

"%PY%" -m app.tools.service stop
echo.
pause
