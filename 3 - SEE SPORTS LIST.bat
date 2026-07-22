@echo off
cd /d "%~dp0"
set PYTHONPATH=src
echo Fetching every sport you can scan...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command ". .\secrets.local.ps1; python -m edge_engine.scan sports | more"
echo.
echo To change sports: open config.yaml and edit the odds_sports list.
pause
