@echo off
rem ---------------------------------------------------------------
rem  Keep this file pure ASCII and do NOT add "chcp": cmd.exe
rem  re-reads the running .bat after a code-page switch, and any
rem  multi-byte character makes it resume at the wrong byte, so
rem  lines get chopped up ("import fastapi" -> "astapi").
rem  All Chinese text is printed by Python (app/main.py) instead.
rem ---------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"

if not exist "%PY%" goto :make_venv

"%PY%" -c "import fastapi, uvicorn, openpyxl, fitz, multipart" >nul 2>&1
if errorlevel 1 goto :install
goto :run

:make_venv
echo [1/2] First run: creating the Python environment (about 1 minute)...
python -m venv .venv
if errorlevel 1 goto :fail
goto :install

:install
echo [2/2] Installing dependencies (first run only, needs internet)...
"%PY%" -m pip install --quiet -r requirements.txt
if errorlevel 1 goto :fail
goto :run

:run
"%PY%" -m app.main
if errorlevel 1 (
    echo.
    echo The service exited with an error - the message above says why.
    echo If it mentions the port is already in use, close the other window first.
    echo.
    pause
)
goto :end

:fail
echo.
echo Failed to start. Please check:
echo   1. Python 3.9 or newer is installed, and was added to PATH
echo      ^(re-run the installer and tick "Add Python to PATH"^)
echo   2. This computer can reach the internet on the first run
echo.
pause
goto :end

:end
endlocal
