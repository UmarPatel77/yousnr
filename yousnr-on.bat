@echo off
REM Turn YouSNR on in the background: no window to keep open. Also turns on by itself at every sign-in.
cd /d "%~dp0"
python yousnr.py autostart on
echo.
timeout /t 15
