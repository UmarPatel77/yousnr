@echo off
REM One-time: installs what YouSNR needs.
cd /d "%~dp0"
python -m pip install --user -U -r requirements.txt
echo.
echo Done. Next: double-click start-yousnr.bat
pause
