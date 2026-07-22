@echo off
cd /d "%~dp0"
set PYTHONPATH=src
echo The most recent signals, newest first.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command ". .\secrets.local.ps1; python -m edge_engine.scan signals | more"
echo.
echo ARB    = locked arithmetic profit, comes with a position size
echo WATCH  = research prompt only, no size - go form your own view
pause
