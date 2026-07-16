#!/bin/bash
# Marriage Photo Selector — launcher. First run creates a venv and installs deps.
cd "$(dirname "$0")"
if [ ! -d ".venv" ]; then
  echo "First run: setting up (one-time, a few minutes)…"
  python3 -m venv .venv
  ./.venv/bin/pip install --upgrade pip -q
  ./.venv/bin/pip install -r requirements.txt
fi
open -g "http://127.0.0.1:8759" 2>/dev/null || true
exec ./.venv/bin/python app.py
