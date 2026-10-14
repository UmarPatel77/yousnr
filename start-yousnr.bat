@echo off
REM YouSNR in a window: ON while this window is open, OFF when you close it.
REM For always-on with no window, use yousnr-on.bat or:  python yousnr.py autostart on
cd /d "%~dp0"
title YouSNR - ON (close this window to turn off)
:loop
python yousnr.py serve
if errorlevel 3 goto done
echo.
echo Server stopped. Restarting in 10 seconds (close this window to turn YouSNR off)...
timeout /t 10 >nul
goto loop
:done
echo.
pause
