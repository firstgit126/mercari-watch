@echo off
title mercari-watch proxy test
cd /d "%~dp0"
echo.
echo Paste your proxy URL below (right-click to paste), then press Enter.
echo Format: http://user:pass@gate.example.com:12345
echo.
set "PROXY="
set /p PROXY=Proxy URL: 
if "%PROXY%"=="" goto empty
echo.
python -m mercari_watch proxy-test --proxy "%PROXY%"
echo.
pause
exit /b 0

:empty
echo.
echo No URL given, nothing to test.
pause
exit /b 2
