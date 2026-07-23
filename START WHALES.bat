@echo off
cd /d "%~dp0"
title Forge Whale Tracker
echo.
echo  WHALE TRACKER
echo  =============
echo.
echo  Watches the wallets in config.yaml and texts you every trade they make
echo  the moment it lands - amount, price, what they bought, and a link.
echo.
echo  Free. Polymarket trades are public.
echo.
echo  In Telegram:  /whales  (who is tracked)   /whale Tony  (his recent trades)
echo.
echo  Closing this window does NOT stop it. Use STOP EVERYTHING.bat.
echo.
start "forge-whales" /min powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "run_whales.ps1"
echo  Started.
echo.
pause
