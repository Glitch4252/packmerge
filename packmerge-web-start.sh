#!/bin/sh
# packmerge-web-start.sh -- launch the Packmerge web UI (Linux equivalent of
# packmerge-web-start.bat). Run it from anywhere; it cds to its own folder
# first, then starts the server without opening a browser (same as the .bat).
# Extra arguments are passed through, e.g.:
#   ./packmerge-web-start.sh --host 0.0.0.0   # reach it from your phone on WiFi
cd "$(dirname "$0")"
exec python3 packmerge_web.py --no-browser "$@"
