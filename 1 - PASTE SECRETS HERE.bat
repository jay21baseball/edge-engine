@echo off
cd /d "%~dp0"
echo Opening your secrets file in Notepad.
echo.
echo Paste your Telegram bot token between the quotes, then SAVE and close.
echo.
start /wait notepad.exe "secrets.local.ps1"
echo Saved. Now run:  2 - CONNECT TELEGRAM.bat
pause
