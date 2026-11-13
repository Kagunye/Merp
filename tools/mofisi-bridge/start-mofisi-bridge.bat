@echo off
REM Start the Mofisi bridge for LAN receipt printers and cash drawers.
REM To start it with Windows: press Win+R, type shell:startup, and put a
REM shortcut to this file in the folder that opens.
REM Change the address below to your Mofisi site.
cd /d "%~dp0"
python mofisi_bridge.py --origin https://your-mofisi-site.example
pause
