@echo off
cd /d "%~dp0"
set PYTHONPATH=src
echo Connecting to Telegram...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command ". .\secrets.local.ps1; python -m edge_engine.setup_telegram"
echo.
pause
