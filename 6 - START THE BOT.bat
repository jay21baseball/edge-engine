@echo off
cd /d "%~dp0"
set PYTHONPATH=src
title Forge bot - keep this window open
echo.
echo  FORGE BOT IS STARTING
echo  =====================
echo.
echo  Keep this window open. Closing it stops the bot.
echo.
echo  In Telegram, message @ForgeCom_bot:
echo.
echo     /dailyedge     best plays to enter today
echo     /weeklyedge    the week's plan
echo     /help          every command
echo.
echo  Press Ctrl+C here to stop.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command ". .\secrets.local.ps1; python -m edge_engine.scan bot"
echo.
echo  Bot stopped.
pause
