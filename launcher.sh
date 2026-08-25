#!/usr/bin/env bash
# parking-printer launcher — sets up venv and starts uvicorn.
# Used by the systemd service unit. Do NOT run manually in normal operation.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
LOG_DIR="$SCRIPT_DIR/logs"

mkdir -p "$LOG_DIR"

# ── Create venv if missing ──────────────────────────────────
if [ ! -d "$VENV_DIR" ]; then
    echo "$(date -Iseconds) | INFO | Creating Python virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

# ── Activate venv ───────────────────────────────────────────
# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"

# ── Install/upgrade dependencies ────────────────────────────
pip install -q --upgrade pip
pip install -q -r "$SCRIPT_DIR/requirements.txt"

# ── Validate .env ───────────────────────────────────────────
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo "$(date -Iseconds) | ERROR | .env file not found at $SCRIPT_DIR/.env"
    echo "Copy .env.txt to .env and fill in your Supabase credentials."
    exit 1
fi

# ── Start uvicorn ───────────────────────────────────────────
echo "$(date -Iseconds) | INFO | Starting parking printer helper..."
exec "$VENV_DIR/bin/uvicorn" \
    printer_helper:app \
    --host 0.0.0.0 \
    --port 8765 \
    --log-level info \
    --access-log \
    2>&1 | tee -a "$LOG_DIR/printer-helper.log"
