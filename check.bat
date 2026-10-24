@echo off
title mercari-watch doctor
cd /d "%~dp0"
echo Running self-check (network / parsing / email). Takes 1-2 minutes...
echo.
python -m mercari_watch doctor %*
echo.
pause
