# Parking Printer Helper

On-site print agent for the Smart Parking Management System.
Receives ESC/POS payloads over HTTP and sends them to the BC-86AC thermal
printer — USB first (`/dev/usb/lp0`), falling back to TCP `PRINTER_IP:9100`.

**v4:** pure push API. No Supabase queue, no Cloudflare Tunnel. The browser
on the same tablet calls `http://localhost:8765` directly; failures are
surfaced in the UI and reprinted manually from Ticket Details.

## Prerequisites

- Python 3.10+ on the tablet's Linux Terminal VM (Debian)
- `PRINT_HELPER_SECRET` shared with the Next.js app
- Printer reachable via LAN (`PRINTER_IP:9100`) or attached via USB/OTG

## Quick Start (Android Linux Terminal VM)

```bash
cd ~/parking-printer        # copy of this folder
bash setup.sh               # venv + deps + systemd user service
nano .env                   # from .env.txt — set secret + printer IP
systemctl --user restart parking-printer
sudo loginctl enable-linger $USER   # autostart at VM boot
```

Verify:

```bash
curl http://127.0.0.1:8765/health
# {"status":"online"}
```

Then on Android Chrome (same tablet): `http://localhost:8765/health` must
also show online — that is the path production printing uses.

## Test print

```bash
curl -X POST http://127.0.0.1:8765/print \
     -H "X-Print-Secret: <SECRET>" \
     -H "Content-Type: application/json" \
     -d '{"ticket":"T-TEST-001","vehicle":"Car","number":"ABC-123","amount":50}'
```

## .env

```
PRINT_HELPER_SECRET=<openssl rand -hex 32>   # must match web app
PRINTER_IP=192.168.18.100
PRINTER_PORT=9100
USB_ENABLED=true
USB_DETECT_INTERVAL=2
```

## API

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/health` | GET | none | Liveness probe only |
| `/status` | GET | secret | Version, printer reachability, USB state, counters |
| `/print-ticket` | POST | secret | Primary print: base64 ESC/POS payload |
| `/print` | POST | secret | Local test receipt |

Full integration contract (web-side flow, audit fields, migration SQL):
see `AGENT-HANDOFF.md`. Deployment runbook: `DEPLOYMENT-GUIDE.md`.

## Troubleshooting

```bash
# Service status / logs
systemctl --user status parking-printer
journalctl --user -u parking-printer -n 30 --no-pager

# Test printer connectivity (LAN mode)
python3 -c "import socket; s=socket.create_connection(('192.168.18.100',9100),5); print('OK'); s.close()"
```
