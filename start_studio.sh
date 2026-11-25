#!/usr/bin/env bash
# Starts Groq Website Studio's browser interface.
cd "$(dirname "$0")" || exit 1
[ -f venv/bin/activate ] && source venv/bin/activate
python studio_gui.py
status=$?
[ -n "$STUDIO_PAUSE" ] && { echo; read -rp "Press Enter to close..."; }
exit $status
