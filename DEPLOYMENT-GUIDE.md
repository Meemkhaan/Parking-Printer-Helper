# Deployment Guide — Parking Printer Helper (New Device / New Client)

Complete runbook for deploying the print system from scratch: tablet setup,
helper install, USB option, app wiring, verification, and troubleshooting.

> **v4 architecture:** direct local printing only. The browser on the tablet
> calls `http://localhost:8765` directly; there is no Supabase queue and no
> Cloudflare Tunnel. Remote printing is intentionally impossible.

---

## Architecture

```
Browser tab on watchman's tablet (app served by Vercel)
   |
   |- ticket created -> POST http://localhost:8765/print-ticket  (~200ms)
   |       Terminal VM port-forward -> helper -> USB (/dev/usb/lp0)
   |                                         or TCP PRINTER_IP:9100
   |
   |- success -> web records: status=printed, printed_at, print_count+1
   '- failure -> web records: status=print_failed -> red banner;
                 reprint later from Ticket Details page

Devices without a helper (phone/office PC): ticket shows on screen only.
```

The helper lives in Android's **Linux Terminal** app (Debian VM). The app
itself needs outbound internet (Vercel + Supabase); printing itself works
even on an isolated LAN.

### What you need before starting

| Item | Notes |
|---|---|
| Tablet with Android Linux Terminal app | Debian VM, must stay powered + on Wi-Fi |
| Thermal printer (BC-86AC or similar) | LAN TCP 9100 **or** USB-attached to the tablet (OTG) |
| Supabase project | For the app's tickets + print audit (`print_count`, `printed_at`) — not used by the helper anymore |

Optional (only if remote access is ever wanted again): a Cloudflare domain +
cloudflared — see appendix. Off by default.

Per client you will choose: **PRINT_HELPER_SECRET**, **PRINTER_IP**, and
which Supabase project the app uses.

---

## Part A — Device prep (tablet Linux Terminal VM)

1. Open the Terminal app, wait for the Debian VM to boot.
2. Install basics:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip curl wget usbutils openssl
```

3. Get the `printer-helper/` folder onto the VM (Terminal app folder sharing
   or git clone). Target layout: `~/parking-printer/`.

---

## Part B — Helper install + configuration

```bash
cd ~/parking-printer

cp .env.txt .env
nano .env
```

```ini
# Fresh random value PER CLIENT:
PRINT_HELPER_SECRET=<output of: openssl rand -hex 32>

PRINTER_IP=<this site's printer LAN IP>
PRINTER_PORT=9100

USB_ENABLED=true
USB_DETECT_INTERVAL=2
```

No quotes around values, no trailing spaces. Save, then:

```bash
bash setup.sh                                  # venv + deps + systemd user service
systemctl --user status parking-printer        # want: active (running)
curl http://127.0.0.1:8765/health              # want: {"status":"online"}

# Autostart at VM boot (if setup.sh linger step warned):
sudo loginctl enable-linger $USER
```

---

## Part C — Verify printing works locally

From inside the VM:

```bash
curl -X POST http://127.0.0.1:8765/print \
     -H "X-Print-Secret: <SECRET>" \
     -H "Content-Type: application/json" \
     -d '{"ticket":"T-TEST-001","vehicle":"Car","number":"ABC-123","amount":50}'
```

Paper feeds via the LAN path if the printer is networked. Then confirm the
browser path (this is what production uses):

1. On the same tablet, open Chrome (Android side) and visit
   `http://localhost:8765/health` — must show `{"status":"online"}`.
   If `localhost` fails, try `http://127.0.0.1:8765`.
2. From any HTTPS page on that tablet you can now fetch the helper directly;
   browsers exempt loopback addresses from mixed-content blocking.

---

## Part D — USB printing (optional)

Only relevant if the printer is physically attached to the tablet via OTG.
**Verify passthrough exists BEFORE relying on it:**

```bash
lsusb                          # printer must be listed
ls -l /dev/usb/lp0             # usblp character device should auto-appear
sudo dmesg | tail -30          # debug if missing
sudo modprobe usblp            # try if driver not loaded
```

If `lsusb` shows nothing, crosvm/AVF does not pass USB through on this
device. Stop here; the LAN path covers everything. No code changes needed
either way — the helper auto-detects `/dev/usb/lp*` every 2s and prefers
USB, falling back to TCP automatically.

Permission fix (if `/dev/usb/lp0` exists):

```bash
sudo usermod -aG lp $USER      # then close & reopen the Terminal app
groups                         # confirm "lp" present
python3 -c "
import printer_helper as p
p.send_to_printer_usb(b'HELLO\n\n\n', p.find_usb_printer())
"
```

After success, `/status` shows `"usb_connected": true` and successful
`/print-ticket` calls report `"path": "usb"`.

---

## Part E — Web app wiring (Next.js agent — see AGENT-HANDOFF.md §4)

1. Vercel env vars: add `NEXT_PUBLIC_PRINT_HELPER_SECRET` = same value as
   the tablet `.env`. Redeploy.
2. Frontend implements the flow in AGENT-HANDOFF.md §4: probe helper,
   POST `/print-ticket` first, then report outcome to your API
   (`printed_at`, `print_count+1`, or `print_failed`) and show a banner on
   failure.
3. One-time DB migration:

```sql
alter table print_jobs add column if not exists print_count integer not null default 0;
alter table print_jobs add column if not exists last_print_path text;
```

4. Verify: create a ticket while watching the printer — row should show
   `status='printed'`, `print_count=1`. Turn the printer off, create another
   one: you get a red banner + `status='print_failed'`. Reprint from Ticket
   Details after power-on: `print_count` becomes 2.

---

## Part F — Final acceptance checklist

- [ ] `systemctl --user status parking-printer` → active
- [ ] `curl http://127.0.0.1:8765/health` from VM → online
- [ ] `http://localhost:8765/health` from Android Chrome → online
- [ ] `/status` without secret header → 401 (auth working)
- [ ] `/status` with secret → correct printer/USB fields
- [ ] Test receipt prints (Part C) via expected path (`usb` or `lan`)
- [ ] Real ticket: prints instantly; row shows `print_count=1`, `printed_at`
- [ ] Failure case: printer off → banner, `status='print_failed'`; reprint
      works after power-on and increments `print_count`
- [ ] Second device (no helper): ticket displays, no crash, no phantom print
- [ ] Tablet reboot → helper auto-starts (linger enabled); cloudflared NOT running

---

## Per-new-client summary (what actually differs)

| Setting | Source |
|---|---|
| `PRINTER_IP` | Client site's LAN — tablet `.env` (skip if USB-only) |
| `PRINT_HELPER_SECRET` | Fresh `openssl rand -hex 32` per client — tablet `.env` AND Vercel `NEXT_PUBLIC_PRINT_HELPER_SECRET` |
| Supabase project | App-level choice; helper is agnostic now |

Everything else is identical across deployments.

---

## Optional: standalone APK (no Linux VM)

If you don't want to run a Linux Terminal VM on a tablet, `apk/` builds the
same helper as a normal Android app via buildozer + GitHub Actions. It uses
Android `UsbManager` (class-7 auto-detect) for USB instead of `/dev/usb/lp0`,
and the same localhost:8765 API. See `apk/README.md`. Functionally identical
to the VM install for the web tier.

## Appendix — Optional remote access (disabled by default)

If a future client needs remote status checks or remote-triggered prints,
re-add a Cloudflare Tunnel per device: install the cloudflared arm64 deb,
`cloudflared tunnel login`, `tunnel create parking-printer-<client>`,
`tunnel route dns ...`, write `/etc/cloudflared/config.yml` (every
placeholder substituted with real values), then
`sudo cloudflared service install`. Remember: enabling this reintroduces
the remote-print capability the current design deliberately excludes.

## Troubleshooting

| Symptom | Cause / Fix |
|---|---|
| Service restart-loop (`exit-code`) | `journalctl --user -u parking-printer -n 30 --no-pager`; usually `.env` typo or broken venv — rerun `bash setup.sh` |
| `401 Invalid or missing X-Print-Secret` despite header | Secret mismatch — compare `grep PRINT_HELPER_SECRET ~/parking-printer/.env` with what the caller sends/Vercel; watch for quotes/spaces; restart helper after editing `.env` |
| `/health` unreachable from Android browser | Terminal VM port-forwarding glitch — toggle the Terminal app off/on; try `127.0.0.1` instead of `localhost` |
| `printer_reachable: false` (LAN mode) | Wrong `PRINTER_IP`, printer off/asleep, different VLAN — verify with `python3 -c "import socket; socket.create_connection(('IP',9100),5); print('OK')"` |
| Prints garbled or duplicated | Payload builder issue (bad ESC/POS init, double send) — inspect payload generation on the web side |
| Helper doesn't start after VM reboot | Linger not enabled — `sudo loginctl enable-linger $USER` |
