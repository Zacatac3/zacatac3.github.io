#!/bin/bash
# Run from any directory: bash /path/to/repo/backend/setup.sh
set -euo pipefail
umask 077

BACKEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-}"
if [ -z "$PYTHON_BIN" ]; then
  if command -v python3.11 >/dev/null 2>&1; then
    PYTHON_BIN=python3.11
  else
    PYTHON_BIN=python3
  fi
fi
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo 'Install Python 3.11 or newer first, then rerun this script.' >&2
  exit 1
fi
"$PYTHON_BIN" -c 'import sys; sys.exit("Python 3.11 or newer is required.") if sys.version_info < (3, 11) else None'

if [ ! -x "$BACKEND_DIR/.venv/bin/python" ]; then
  "$PYTHON_BIN" -m venv "$BACKEND_DIR/.venv"
fi
"$BACKEND_DIR/.venv/bin/python" -c 'import sys; sys.exit("Recreate .venv with Python 3.11 or newer.") if sys.version_info < (3, 11) else None'
"$BACKEND_DIR/.venv/bin/python" -m pip install -r "$BACKEND_DIR/requirements.txt"
"$BACKEND_DIR/.venv/bin/python" -m pip check
"$BACKEND_DIR/.venv/bin/python" -c 'import flask, flask_cors, pywebpush, requests; from zoneinfo import ZoneInfo; ZoneInfo("America/New_York")'

for name in private_key.pem admin_token.txt subscription.json polling.json cooldown.json; do
  if [ -f "$BACKEND_DIR/$name" ]; then
    chmod 600 "$BACKEND_DIR/$name"
  fi
done

if ! command -v cloudflared >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    brew install cloudflared
  else
    echo 'Python dependencies are ready. Install cloudflared separately, or install Homebrew from https://brew.sh and rerun this script.' >&2
    exit 1
  fi
fi

echo 'Setup complete. No monitoring or inventory requests were started.'
if [ ! -f "$BACKEND_DIR/private_key.pem" ]; then
  echo 'Before starting: privately copy private_key.pem from your old Mac into backend/.'
fi
if [ ! -f "$BACKEND_DIR/subscription.json" ]; then
  echo 'Copy subscription.json from your old Mac, or enable iPhone notifications again.'
fi
printf 'Start Python: "%s/.venv/bin/python" "%s/monitor.py"\n' "$BACKEND_DIR" "$BACKEND_DIR"
echo 'In another terminal: cloudflared tunnel --url http://127.0.0.1:8766'
