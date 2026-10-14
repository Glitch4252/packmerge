@echo off
rem Starts the packmerge web UI silently in the background.
rem
rem Setup (once):
rem   1. Put this file in your packmerge folder, next to packmerge_web.py
rem   2. Win+R, type  shell:startup  and press Enter
rem   3. Drop this file (or a shortcut to it) into that Startup folder
rem
rem From then on the web UI is up at http://127.0.0.1:8471 every time
rem you log in, with no console window. If `pyw` isn't recognized,
rem replace it below with the full path to pythonw.exe, e.g.
rem   "C:\Users\You\AppData\Local\Programs\Python\Python313\pythonw.exe"
cd /d "%~dp0"
pyw packmerge_web.py --no-browser
