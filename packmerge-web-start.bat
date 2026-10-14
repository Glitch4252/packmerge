@echo off
rem Starts the packmerge web UI silently in the background.
rem
rem Setup (once):
rem   1. Put this file in your packmerge folder, next to packmerge_web.py
rem   2. Win+R, type  shell:startup  and press Enter
rem   3. Drop this file (or a shortcut to it) into that Startup folder
rem
rem From then on the web UI is up at http://127.0.0.1:8471 every time
rem you log in. If it can't start, it leaves a PACKMERGE-WEB-*.txt file
rem next to itself explaining why.
cd /d "%~dp0"

if not exist "packmerge_web.py" (
    echo Put this .bat file in your packmerge folder, next to packmerge_web.py. > "%~dp0PACKMERGE-WEB-SETUP-ERROR.txt"
    exit /b 1
)

where pyw >nul 2>nul
if not errorlevel 1 (
    pyw packmerge_web.py --no-browser
    exit /b 0
)

where pythonw >nul 2>nul
if not errorlevel 1 (
    pythonw packmerge_web.py --no-browser
    exit /b 0
)

rem last resort: minimized console window instead of hidden
start "packmerge web" /min python packmerge_web.py --no-browser
