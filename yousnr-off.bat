@echo off
REM Turn YouSNR off. Add "autostart off" to also stop it turning on at sign-in:
REM   python yousnr.py autostart off
cd /d "%~dp0"
python yousnr.py stop
echo.
timeout /t 15
