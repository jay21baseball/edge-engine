@echo off
cd /d "%~dp0"
set PYTHONPATH=src
echo Scanning Kalshi and Polymarket. This takes about a minute.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command ". .\secrets.local.ps1; python -m edge_engine.scan scan"
echo.
echo Done. If anything cleared the threshold it was sent to Telegram.
echo Finding nothing is normal and correct - see the README.
pause
