@echo off
cd /d "%~dp0"
title Forge Live Recorder
echo.
echo  LIVE DIVERGENCE RECORDER
echo  =======================
echo.
echo  Records ESPN live win probability vs Polymarket price every 60s during
echo  games. Free - no paid data. It only observes and logs; it does NOT alert
echo  yet, because it is still proving whether the gaps are real and reachable.
echo.
echo  Best run during a slate of MLB / NBA / WNBA / NFL games.
echo.
echo  In Telegram:  /live  (gaps now)   /livestats  (the verdict so far)
echo.
echo  Closing this window does NOT stop it. Use STOP EVERYTHING.bat.
echo.
start "forge-live" /min powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "run_live.ps1"
echo  Started.
echo.
pause
