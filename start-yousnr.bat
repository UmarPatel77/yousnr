@echo off
REM YouSNR on/off switch. Double-click to turn ON. Close this window to turn OFF.
REM While ON: new videos are fetched for anyone visiting yousnr.com (via the Cloudflare Tunnel).
REM While OFF: videos already on yousnr.com still work; new ones show "server offline".
cd /d "%~dp0"
title YouSNR - ON (close this window to turn off)
:loop
python yousnr.py serve
echo.
echo Server stopped. Restarting in 10 seconds (close this window to turn YouSNR off)...
timeout /t 10 >nul
goto loop
