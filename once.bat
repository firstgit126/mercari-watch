@echo off
title mercari-watch once
cd /d "%~dp0"
echo Running one cycle...
echo.
python -m mercari_watch once %*
echo.
pause
