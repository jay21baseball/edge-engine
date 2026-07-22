@echo off
cd /d "%~dp0"
echo Stopping Forge...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*edge_engine.scan*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; 'stopped'"
echo.
pause
