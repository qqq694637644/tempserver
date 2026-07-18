@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
) else (
    python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
)
if errorlevel 1 (
    echo.
    echo tempserver failed to start or stopped with an error.
    if not defined TEMPSERVER_SERVICE_MODE pause
)

