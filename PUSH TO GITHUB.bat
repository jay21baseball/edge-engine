@echo off
cd /d "%~dp0"
title Push to GitHub
echo.
echo  PUSHING TO GITHUB
echo  =================
echo.
echo  A browser window will open asking you to sign in to GitHub.
echo  Sign in and approve. It only asks once - after that it remembers.
echo.
git push -u origin main
echo.
if errorlevel 1 (
  echo  Push FAILED. Copy the message above and send it to Claude.
) else (
  echo  Pushed successfully. Tell Claude and it will take over from here.
)
echo.
pause
