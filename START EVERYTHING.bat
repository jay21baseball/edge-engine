@echo off
cd /d "%~dp0"
title Forge - control window
echo.
echo  STARTING FORGE
echo  ==============
echo.
echo  Two background processes:
echo    1. Scanner  - scans both venues, pushes alerts when something big appears
echo    2. Bot      - answers your commands in Telegram
echo.

rem Both must be launched with "start" so they run in parallel — a bare
rem powershell call blocks and the second process would never start.
start "forge-scanner" /min powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "run_watch.ps1"
start "forge-bot"     /min powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "run_bot.ps1"

echo  Started. Logs are in the data folder.
echo.
echo  In Telegram, message @ForgeCom_bot:
echo     /dailyedge   best plays to enter today
echo     /whynot      what got rejected, and why
echo     /help        every command
echo.
echo  Closing this window does NOT stop them.
echo  To stop: run "STOP EVERYTHING.bat"
echo.
pause
exit /b 0

:fail
echo  Failed to start. Check that Python is installed and secrets.local.ps1 exists.
pause
