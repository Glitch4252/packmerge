@echo off
rem Starts the packmerge web UI silently in the background.
rem 1. Put this file in your packmerge folder, next to packmerge_web.py
rem 2. Win+R, type  shell:startup, press Enter
rem 3. Drop this file (or a shortcut to it) into that Startup folder
rem The site is then up at http://127.0.0.1:8471 every time you log in.
cd /d "%~dp0"
pyw packmerge_web.py --no-browser
