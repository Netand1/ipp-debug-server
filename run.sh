#!/bin/bash
# IPP Debug Server launcher
# Uses ./.venv if present (create it with: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt)
cd "$(dirname "$0")"
if [ -f .venv/bin/activate ]; then
    source .venv/bin/activate
fi
exec python3 app.py "$@"
