@echo off
REM Is YouSNR on? Tunnel, spending this month, and the last lines of yousnr.log.
cd /d "%~dp0"
python yousnr.py status
echo.
pause
