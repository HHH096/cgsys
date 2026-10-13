@echo off
rem Publish the local server through a Cloudflare tunnel (temporary URL).
rem For the permanent (fixed URL) setup, see the guide document in this folder.
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

"%PY%" -m app.tools.tunnel %*
echo.
pause
