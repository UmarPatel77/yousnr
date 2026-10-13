@echo off
REM YouSNR server: fetches transcripts for new videos on demand. Keep this window open.
cd /d "%~dp0"
title YouSNR server
:loop
python yousnr.py serve
echo.
echo Server stopped. Restarting in 10 seconds (close this window to quit)...
timeout /t 10 >nul
goto loop
