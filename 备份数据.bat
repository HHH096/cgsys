@echo off
rem Keep this file ASCII-only and do NOT add "chcp" (see the design doc, section 10).
setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] No runtime environment found.
    echo         Run the launcher once first, then try again.
    pause
    exit /b 1
)

"%PY%" -m app.tools.backup %*
echo.
pause
