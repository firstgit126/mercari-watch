@echo off
title mercari-watch running
cd /d "%~dp0"
echo Watching. Closing this window stops it.
echo For auto-start on login use scripts\install_task.ps1
echo.
python -m mercari_watch run %*
if errorlevel 1 pause
