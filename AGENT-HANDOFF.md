# Agent Handoff — Printer Helper Integration Status

> **Audience:** the opencode/AI agent working inside the Parking Management
> System's Next.js repository. Read this before touching any printing-related
> code. It documents what exists, why, the exact API contract, and the rules
> for modifying either side.

---

## 1. What this component is & current architecture (v4)

`printer-helper/` is the on-site print agent for the Smart Parking Management
System. It runs on the watchman's Android tablet inside the built-in **Linux
Terminal** app (a real Debian VM — *not* Termux). It receives ESC/POS receipt
payloads and drives the BC-86AC 80mm thermal printer.

**v4 design decisions (user-approved):**

1. **Local-first direct printing.** The browser tab running on the same
   tablet calls the helper directly at `http://localhost:8765` (the Terminal
   app forwards VM ports to Android). No Vercel round-trip, no Cloudflare
   Tunnel on the happy path. ~200ms prints.
2. **No fallback queue.** If printing fails (printer off, helper down), the
   UI shows the ticket on screen with a "print failed" state; staff reprint
   manually from the Ticket Details page. The Supabase `pending→printing`
   queue machinery was removed from the helper entirely.
3. **No remote printing.** Cloudflared is disabled/removed — there is no
   public ingress to the helper. Printing can only originate from devices on
   the local network (practically: the tablet itself).
4. **USB-first routing.** If `/dev/usb/lp*` appears (printer via OTG),
   output writes straight to USB; otherwise TCP to `PRINTER_IP:9100`.
   Automatic per print; invisible to the web tier.
5. **Audit is the web tier's job.** Every print attempt's outcome must be
   recorded against the ticket in Supabase by the frontend/server actions
   (see §4) — including a **print count per receipt** for auditing.

```
Browser (tablet)
   ├─ POST http://localhost:8765/print-ticket ──► helper ──► USB or LAN printer
   │        success → report {printed} to Vercel API → print_count+1, printed_at
   │        failure → report/print-failed state → red banner, reprint available
   └─ Devices without a helper: ticket shows on screen only; tablet reprints later
```

---

## 2. Files created/modified across sessions (and purpose)

| File | Change | Purpose |
|---|---|---|
| `printer_helper.py` | v2.0.0 → v3.0.0 → **v4.0.0** | v3 added push endpoint, secret auth, real reachability probe, USB auto-routing. **v4 removed the entire Supabase poller/queue** → pure push API |
| `.env.txt` | Rewritten | Only helper-relevant vars remain (secret, printer IP/port, USB flags); Supabase/poller vars removed |
| `requirements.txt` | Updated | `requests` dependency dropped (no more Supabase HTTP calls) |
| `INSTALL.md`, `DEPLOYMENT-GUIDE.md` | Created/updated | Install steps; full new-device/new-client runbook incl. troubleshooting from first live deployment |
| `AGENT-HANDOFF.md` | This file | Living integration contract |

---

## 3. Deployment / migration state

- Tablet currently still runs **v3** until the updated folder is copied over
  and the service restarted (`bash setup.sh` idempotent, then
  `systemctl --user restart parking-printer`). Verify with
  `curl -H "X-Print-Secret: ..." http://localhost:8765/status` → `"version":"4.0.0"`.
- Cloudflare Tunnel exists but is being decommissioned
  (`sudo systemctl disable --now cloudflared`). Do not rely on
  `https://parking-printer.muzammilcarparking.com` in new code.
- **Web-side migration is REQUIRED and not yet done** (see §4). The old
  fields `pending_jobs`, `supabase_configured`, `last_poll_at`,
  `poll_interval` no longer exist in `/status`.

---

## 4. API contract (consume this, don't re-invent)

Base URL from the tablet's browser: `http://localhost:8765`
(fallback `http://127.0.0.1:8765` if `localhost` misbehaves).
Probe both cheaply — see client snippet below.

### Auth

All endpoints except `/health` require header:

```
X-Print-Secret: <PRINT_HELPER_SECRET>
```

Wrong/missing → `401 {"detail":"Invalid or missing X-Print-Secret"}`.
A browser GET of `/status` returning 401 is correct behavior.

The secret ships to the browser as `NEXT_PUBLIC_PRINT_HELPER_SECRET`.
This is acceptable *because* there is no public ingress — keep it that way.

### Endpoints

| Endpoint | Method | Auth | Request | Success | Errors |
|---|---|---|---|---|---|
| `/health` | GET | none | — | `{"status":"online"}` | liveness only, never probes printer |
| `/status` | GET | secret | — | shape below | 401 |
| `/print-ticket` | POST | secret | `{"payload_base64": "...", "ticket_number": "T-123"}` | `{"success": true, "path": "usb"\|"lan"}` | 400 invalid payload · 503 print failed · 401 |
| `/print` | POST | secret | `{"ticket","vehicle","number","amount"}` | `{"success":true,...}` | 500 — test receipt generator, LAN only |

### `/status` response (v4)

```json
{
  "status": "online",
  "version": "4.0.0",
  "uptime_seconds": 12345,
  "printer": "192.168.18.100",
  "printer_port": 9100,
  "printer_reachable": false,
  "usb_connected": true,
  "usb_device_path": "/dev/usb/lp0",
  "active_print_path": "usb",
  "last_printed_job_id": "T-77",
  "last_printed_via": "usb",
  "last_error": null,
  "jobs_processed": 42,
  "jobs_failed": 1
}
```

### Required web-side flow ("first print", audit, multi-device)

On ticket creation (and on every Reprint action):

1. Build ESC/POS payload base64.
2. Probe helper: `fetch('http://localhost:8765/health')` with ~800ms timeout;
   try `127.0.0.1` if that fails.
3. **Print first**: `POST /print-ticket`. Treat 503/network error as failure.
4. Report outcome to your API (server action / route handler → Supabase):
   - success → `UPDATE print_jobs SET status='printed', printed_at=now(),
     last_print_path=<path>, print_count = print_count + 1 WHERE id=<ticketId>`
   - failure → `UPDATE ... SET status='print_failed', last_error=<detail>`
     (leave printed_at/print_count untouched)
5. UI: failure ⇒ banner "Printer not reachable — reprint from Ticket Details".

**Schema addition needed:**
```sql
alter table print_jobs add column if not exists print_count integer not null default 0;
alter table print_jobs add column if not exists last_print_path text;
```
(`printed_at`, `error` already exist.) Status values now effectively:
`created → printed | print_failed`. Old queue values
(`pending`/`printing`) are obsolete.

**Multi-device:** any device without the helper (owner's phone, office PC)
gets step-4 failure automatically → ticket displays on screen; the watchman
reprints it later from Ticket Details on the tablet. No code special-casing
needed beyond the generic failure path.

**Client probe sketch:**

```ts
const BASES = ["http://localhost:8765", "http://127.0.0.1:8765"];

async function findHelper(): Promise<string | null> {
  for (const b of BASES) {
    try {
      const ctl = new AbortController();
      const t = setTimeout(() => ctl.abort(), 800);
      const r = await fetch(`${b}/health`, { signal: ctl.signal });
      clearTimeout(t);
      if (r.ok) return b;
    } catch {}
  }
  return null;
}
```

---

## 5. Environment variables

Helper side (tablet `.env`):

| Var | Default | Notes |
|---|---|---|
| `PRINT_HELPER_SECRET` | unset = all protected endpoints 401 | Per-client `openssl rand -hex 32`; must match web app |
| `PRINTER_IP` / `PRINTER_PORT` | `192.168.18.100` / `9100` | LAN path |
| `PRINTER_TIMEOUT` / `PRINTER_PROBE_TIMEOUT` | `5` / `1.5` | optional |
| `USB_ENABLED` / `USB_DETECT_INTERVAL` | `true` / `2` | optional |

Web side:

| Var | Value |
|---|---|
| `NEXT_PUBLIC_PRINT_HELPER_SECRET` | identical to helper |
| (existing Supabase vars unchanged) | used for tickets + audit updates |

## 6. Rules for future modifications

- Do NOT reintroduce the Supabase queue or Cloudflare Tunnel without an
  explicit user request — their removal was deliberate (latency + no-remote-print policy).
- Keep `require_secret` on any new endpoint that prints or reveals network detail.
- Helper changes: `python -m py_compile printer_helper.py`, copy to tablet,
  `systemctl --user restart parking-printer`, verify `/health` + `/status`.
- After changing the contract, update this file AND `DEPLOYMENT-GUIDE.md`.

## 7. Where to look next time

- `DEPLOYMENT-GUIDE.md` — install/runbook/troubleshooting
- `apk/` — standalone Android APK build (buildozer + CI) for tablets that
  shouldn't run a full Linux VM; same API contract, USB via `UsbManager`.
  See `apk/README.md`.
- Helper logs on tablet: `journalctl --user -u parking-printer -n 50 --no-pager`
- USB verification: `lsusb` + `ls -l /dev/usb/lp0` inside the VM (passthrough unconfirmed on current device; absence is fine — LAN path takes over)
