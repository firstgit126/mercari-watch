@echo off
title mercari-watch probe
cd /d "%~dp0"
echo Probing direct access to Mercari (no proxy). About 1 minute...
echo.
python tools\probe_network.py %*
echo.
pause
