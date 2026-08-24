#!/usr/bin/env bash
# One-time setup for the parking printer helper on ChromeOS Linux (Crostini).
# Run this once: bash setup.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_DIR="$HOME/.config/systemd/user"
HELPER_LINK="$HOME/parking-printer"

echo "=== Parking Printer Helper — Setup ==="

# ── 1. Create ~/parking-printer symlink ─────────────────────
if [ ! -L "$HELPER_LINK" ]; then
    ln -sfn "$SCRIPT_DIR" "$HELPER_LINK"
    echo "  ✓ Linked $HELPER_LINK → $SCRIPT_DIR"
else
    echo "  ✓ Symlink already exists"
fi

# ── 2. Create .env from template if missing ────────────────
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    cp "$SCRIPT_DIR/.env.txt" "$SCRIPT_DIR/.env"
    echo "  ✓ Created .env — edit it with your Supabase credentials:"
    echo "    nano $SCRIPT_DIR/.env"
fi

# ── 3. Make launcher executable ─────────────────────────────
chmod +x "$SCRIPT_DIR/launcher.sh"

# ── 4. Install systemd user service ────────────────────────
mkdir -p "$SERVICE_DIR"
cp "$SCRIPT_DIR/parking-printer.service" "$SERVICE_DIR/parking-printer.service"
systemctl --user daemon-reload 2>/dev/null || true
echo "  ✓ Installed systemd user service"

# ── 5. Enable linger (service starts at container boot) ────
if command -v loginctl >/dev/null 2>&1; then
    sudo loginctl enable-linger "$USER" 2>/dev/null || {
        echo "  ⚠ Could not enable linger (needs sudo). Run:"
        echo "    sudo loginctl enable-linger $USER"
    }
fi

# ── 6. Enable and start the service ────────────────────────
systemctl --user enable parking-printer.service 2>/dev/null || true
systemctl --user start parking-printer.service 2>/dev/null || true
echo "  ✓ Service enabled and started"

# ── 7. Verify ───────────────────────────────────────────────
echo ""
echo "=== Verification ==="
sleep 2
if systemctl --user is-active --quiet parking-printer.service; then
    echo "  ✓ Service is running"
else
    echo "  ✗ Service failed to start — check logs:"
    echo "    journalctl --user -u parking-printer.service -n 30"
fi

echo ""
echo "=== Useful commands ==="
echo "  systemctl --user status parking-printer    # check status"
echo "  systemctl --user restart parking-printer   # restart"
echo "  journalctl --user -u parking-printer -f    # live logs"
echo "  curl http://127.0.0.1:8765/status          # API status"
echo ""
