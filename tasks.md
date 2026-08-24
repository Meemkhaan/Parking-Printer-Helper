TASK 1: Replace polling-based printer bridge with direct push + tunnel, and add real connectivity detection.

CONTEXT
My Next.js app is deployed on Vercel. My thermal receipt printer (TCP/9100, 
ESC/POS) is on a local network that Vercel can't reach — it's a private LAN 
IP. Currently a Python FastAPI helper (printer_helper.py, below) polls a 
Supabase `print_jobs` table every ~2s and sends jobs to the printer over TCP. 
This adds ~1-3s latency per print and the helper has no way to report 
whether the printer is actually reachable — `/status` only reports config 
values and Supabase job counts, never opens a socket to the printer.

Two separate problems to fix:
1. Vercel (and local dev) cannot reach the printer helper's LAN IP directly — 
   need a public ingress via Cloudflare Tunnel.
2. Printing should push directly to the helper the instant a ticket is 
   built, instead of waiting for the poller to notice a queued row. Keep 
   the Supabase queue only as a fallback for when the tunnel/printer is 
   unreachable.

STEP 1 — Cloudflare Tunnel (infra, do this manually, not code)
On the machine running printer_helper.py:
- Install cloudflared, `cloudflared tunnel login`
- `cloudflared tunnel create parking-printer`
- Point a hostname at it: `cloudflared tunnel route dns parking-printer parking-printer.<mydomain>`
- Run `cloudflared tunnel run parking-printer` pointed at the FastAPI port (e.g. localhost:8000), kept running alongside the FastAPI process (systemd unit or equivalent)
This gives a stable public HTTPS URL that proxies to the local helper, 
reachable identically from Vercel and from `next dev` regardless of network.

STEP 2 — Create/replace printer_helper.py with this full file:

```python
import base64
import hmac
import logging
import os
import signal
import socket
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import requests
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
PRINTER_IP = os.getenv("PRINTER_IP", "192.168.18.100")
PRINTER_PORT = int(os.getenv("PRINTER_PORT", "9100"))
PRINT_HELPER_SECRET = os.getenv("PRINT_HELPER_SECRET", "")
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "2"))
JOB_LIMIT = int(os.getenv("JOB_LIMIT", "5"))
PRINTER_TIMEOUT = float(os.getenv("PRINTER_TIMEOUT", "5"))
MAX_POLL_BACKOFF = float(os.getenv("MAX_POLL_BACKOFF", "8"))
PRINTER_PROBE_TIMEOUT = float(os.getenv("PRINTER_PROBE_TIMEOUT", "1.5"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("parking-printer")

if not SUPABASE_URL:
    logger.warning("SUPABASE_URL is not configured.")
if not SUPABASE_ANON_KEY:
    logger.warning("SUPABASE_ANON_KEY is not configured.")
if not PRINT_HELPER_SECRET:
    logger.warning("PRINT_HELPER_SECRET is not set — endpoints will refuse all requests once exposed publicly.")

def require_secret(x_print_secret: str = Header(default="")):
    if not PRINT_HELPER_SECRET or not hmac.compare_digest(x_print_secret, PRINT_HELPER_SECRET):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Print-Secret")

worker_running = True
last_poll_at: Optional[datetime] = None
last_error: Optional[str] = None
last_printed_job_id: Optional[str] = None
jobs_processed: int = 0
jobs_failed: int = 0
state_lock = threading.Lock()

def _shutdown_handler(signum, frame):
    global worker_running
    logger.info("Received signal %d — shutting down.", signum)
    worker_running = False

signal.signal(signal.SIGTERM, _shutdown_handler)
signal.signal(signal.SIGINT, _shutdown_handler)

class PrintJobRequest(BaseModel):
    ticket: str
    vehicle: str
    number: str
    amount: float

class PrintTicketRequest(BaseModel):
    payload_base64: str
    ticket_number: Optional[str] = None

def send_to_printer(data: bytes):
    if not data:
        raise ValueError("Print payload is empty.")
    logger.info("Sending %d bytes to printer %s:%d", len(data), PRINTER_IP, PRINTER_PORT)
    with socket.create_connection((PRINTER_IP, PRINTER_PORT), timeout=PRINTER_TIMEOUT) as printer:
        printer.sendall(data)
        try:
            printer.shutdown(socket.SHUT_WR)
        except Exception:
            pass
        time.sleep(0.2)
    logger.info("Printer accepted %d bytes.", len(data))

def check_printer_reachable(timeout: float = PRINTER_PROBE_TIMEOUT) -> bool:
    try:
        with socket.create_connection((PRINTER_IP, PRINTER_PORT), timeout=timeout):
            return True
    except OSError:
        return False

def create_test_receipt(job: PrintJobRequest) -> bytes:
    ESC = b"\x1b"
    GS = b"\x1d"
    data = bytearray()
    data += ESC + b"@"
    data += ESC + b"a" + b"\x01"
    data += b"PARKING RECEIPT\n"
    data += b"--------------------------------\n"
    data += ESC + b"a" + b"\x00"
    data += f"Ticket : {job.ticket}\n".encode()
    data += f"Vehicle: {job.vehicle}\n".encode()
    data += f"Number : {job.number}\n".encode()
    data += f"Amount : Rs. {job.amount:.2f}\n".encode()
    data += b"--------------------------------\n"
    data += ESC + b"a" + b"\x01"
    data += b"Thank You\n"
    data += b"\n\n\n"
    data += GS + b"V" + b"\x00"
    return bytes(data)

def supabase_headers():
    if not SUPABASE_ANON_KEY:
        raise RuntimeError("SUPABASE_ANON_KEY is not configured.")
    return {"apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {SUPABASE_ANON_KEY}", "Content-Type": "application/json"}

def claim_next_job() -> Optional[dict]:
    url = (f"{SUPABASE_URL}/rest/v1/print_jobs?select=id,payload_base64&status=eq.pending&order=created_at.asc&limit=1")
    response = requests.get(url, headers=supabase_headers(), timeout=8)
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"Supabase GET returned {response.status_code}: {response.text[:500]}")
    jobs = response.json()
    if not isinstance(jobs, list) or not jobs:
        return None
    job = jobs[0]
    job_id = job.get("id")
    if not job_id:
        return None
    patch_url = f"{SUPABASE_URL}/rest/v1/print_jobs?id=eq.{job_id}&status=eq.pending"
    patch_response = requests.patch(patch_url, headers={**supabase_headers(), "Prefer": "return=representation"}, json={"status": "printing"}, timeout=8)
    if not 200 <= patch_response.status_code < 300:
        raise RuntimeError(f"Supabase PATCH (claim) returned {patch_response.status_code}: {patch_response.text[:500]}")
    claimed = patch_response.json()
    if not isinstance(claimed, list) or not claimed:
        return None
    return job

def fetch_pending_jobs_count() -> int:
    url = f"{SUPABASE_URL}/rest/v1/print_jobs?select=id&status=eq.pending"
    response = requests.get(url, headers={**supabase_headers(), "Prefer": "count=exact"}, timeout=8)
    if "content-range" in response.headers:
        parts = response.headers["content-range"].split("/")
        if len(parts) == 2 and parts[1] != "*":
            return int(parts[1])
    return 0

def update_job_status(job_id: str, status: str, error: Optional[str] = None):
    url = f"{SUPABASE_URL}/rest/v1/print_jobs?id=eq.{job_id}"
    body: dict = {"status": status}
    if error is not None:
        body["error"] = error[:1000]
    if status == "done":
        body["printed_at"] = datetime.now(timezone.utc).isoformat()
    response = requests.patch(url, headers={**supabase_headers(), "Prefer": "return=minimal"}, json=body, timeout=8)
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"Supabase PATCH returned {response.status_code}: {response.text[:500]}")

def process_job(job: dict):
    global jobs_processed, jobs_failed
    job_id = job.get("id")
    payload_base64 = job.get("payload_base64")
    if not job_id:
        logger.error("Print job has no id — skipping.")
        return
    if not payload_base64:
        logger.error("Print job %s has empty payload_base64 — marking error.", job_id)
        try:
            update_job_status(job_id, "error", "Empty payload_base64")
        except Exception:
            logger.exception("Could not update job %s to error.", job_id)
        jobs_failed += 1
        return
    logger.info("Processing print job: %s", job_id)
    try:
        print_bytes = base64.b64decode(payload_base64, validate=True)
    except Exception as exc:
        error_message = f"Invalid base64 payload: {exc}"
        logger.error("Job %s: %s", job_id, error_message)
        try:
            update_job_status(job_id, "error", error_message)
        except Exception:
            logger.exception("Could not update job %s to error.", job_id)
        jobs_failed += 1
        return
    if not print_bytes:
        logger.error("Job %s decoded to zero bytes.", job_id)
        try:
            update_job_status(job_id, "error", "Decoded to zero bytes")
        except Exception:
            logger.exception("Could not update job %s to error.", job_id)
        jobs_failed += 1
        return
    logger.info("Job %s decoded to %d bytes.", job_id, len(print_bytes))
    try:
        send_to_printer_auto(print_bytes)
        update_job_status(job_id, "done")
        global last_printed_job_id
        with state_lock:
            last_printed_job_id = job_id
            jobs_processed += 1
        logger.info("Print job completed: %s", job_id)
    except Exception as exc:
        error_message = str(exc) or "Print failed."
        logger.exception("Print failed for job %s", job_id)
        try:
            update_job_status(job_id, "error", error_message)
        except Exception:
            logger.exception("Could not update job %s to error.", job_id)
        with state_lock:
            jobs_failed += 1

def poll_once():
    global last_poll_at, last_error
    try:
        job = claim_next_job()
        with state_lock:
            last_poll_at = datetime.now(timezone.utc)
            last_error = None
        if job:
            logger.info("Claimed print job: %s", job.get("id"))
            process_job(job)
    except Exception as exc:
        with state_lock:
            last_poll_at = datetime.now(timezone.utc)
            last_error = str(exc)
        logger.warning("Supabase poll failed: %s", exc)

def poll_loop():
    backoff = POLL_INTERVAL
    logger.info("Supabase fallback poller started. Polling every %.1fs (max backoff %.1fs).", POLL_INTERVAL, MAX_POLL_BACKOFF)
    while worker_running:
        poll_once()
        with state_lock:
            has_error = last_error is not None
        backoff = min(backoff * 2, MAX_POLL_BACKOFF) if has_error else POLL_INTERVAL
        time.sleep(backoff)
    logger.info("Supabase fallback poller stopped.")

_worker_thread: Optional[threading.Thread] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global worker_running, _worker_thread
    worker_running = True
    _worker_thread = threading.Thread(target=poll_loop, name="supabase-fallback-poller", daemon=True)
    _worker_thread.start()
    logger.info("Fallback poller thread started.")
    if USB_ENABLED:
        threading.Thread(target=usb_detect_loop, name="usb-detect", daemon=True).start()
        logger.info("USB detect thread started.")
    yield
    worker_running = False
    if _worker_thread:
        _worker_thread.join(timeout=5)
    logger.info("Fallback poller thread stopped.")

app = FastAPI(title="Parking Printer Helper", version="3.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])

@app.get("/health")
def health():
    return {"status": "online"}

@app.get("/status", dependencies=[Depends(require_secret)])
def status():
    with state_lock:
        poll_time = last_poll_at.isoformat() if last_poll_at else None
        error = last_error
        printed = last_printed_job_id
        processed = jobs_processed
        failed = jobs_failed
        usb_path = usb_device_path
        active_path = active_print_path
    pending = 0
    try:
        pending = fetch_pending_jobs_count()
    except Exception:
        pass
    return {
        "status": "online",
        "printer": PRINTER_IP,
        "printer_port": PRINTER_PORT,
        "printer_reachable": check_printer_reachable(),
        "usb_connected": usb_path is not None,
        "usb_device_path": usb_path,
        "active_print_path": active_path,
        "supabase_configured": bool(SUPABASE_URL and SUPABASE_ANON_KEY),
        "poll_interval": POLL_INTERVAL,
        "last_poll_at": poll_time,
        "last_error": error,
        "last_printed_job_id": printed,
        "pending_jobs": pending,
        "jobs_processed": processed,
        "jobs_failed": failed,
    }

@app.post("/print-ticket", dependencies=[Depends(require_secret)])
def print_ticket(job: PrintTicketRequest):
    try:
        print_bytes = base64.b64decode(job.payload_base64, validate=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid base64 payload: {exc}")
    if not print_bytes:
        raise HTTPException(status_code=400, detail="Decoded to zero bytes")
    try:
        send_to_printer_auto(print_bytes)
    except Exception as exc:
        logger.exception("Direct print failed for %s", job.ticket_number or "(unknown)")
        raise HTTPException(status_code=503, detail=str(exc))
    with state_lock:
        global jobs_processed, last_printed_job_id
        jobs_processed += 1
        last_printed_job_id = job.ticket_number or last_printed_job_id
    return {"success": True}

@app.post("/print", dependencies=[Depends(require_secret)])
def print_local(job: PrintJobRequest):
    try:
        receipt = create_test_receipt(job)
        send_to_printer(receipt)
        return {"success": True, "message": "Receipt sent to printer"}
    except Exception as exc:
        logger.exception("Local print failed.")
        raise HTTPException(status_code=500, detail=str(exc))
```

Note: this file references `usb_device_path`, `active_print_path`,
`usb_detect_loop`, `USB_ENABLED`, and `send_to_printer_auto` — those are
added in TASK 2 below. Implement TASK 2's Step 1 additions into this same
file before it will run; `process_job` and `print_ticket` already call
`send_to_printer_auto` (not the plain `send_to_printer`) so that USB-first
routing applies to both the fast path and the fallback queue once TASK 2 is
in place.

ENV VARS NEEDED (Task 1)
PRINT_HELPER_SECRET=<random-string>
(Vercel side, separate app — see below)

---

TASK 2: Add USB printer auto-detection as the primary print path, keeping the existing LAN/TCP path as automatic backup.

CONTEXT
The printer_helper.py from Task 1 runs inside Android's built-in "Linux
Terminal" app — a real Debian VM (via the Android Virtualization Framework /
crosvm), not Termux. This matters: Termux's USB access goes through
Android's UsbManager sandbox and needs an awkward fd-wrapping dance via
libusb_wrap_sys_device(). A genuine Linux VM with real USB passthrough does
NOT need any of that — if the kernel can see the device at all, its usblp
driver auto-binds any USB Printer Class device to /dev/usb/lp0, and printing
is just a raw file write. But it is NOT CONFIRMED that this VM's hypervisor
(crosvm/AVF) currently passes USB devices into the VM at all — this is a
known historical gap in this exact class of sandboxed VM (ChromeOS's
Crostini, built on the same crosvm technology, went about two years without
USB support before it was added), and Android's Terminal app is much newer.

STEP 0 — VERIFY FIRST, DO NOT SKIP THIS
Before writing any code, confirm USB passthrough actually works in this
environment:
  sudo apt update && sudo apt install -y usbutils
  lsusb
With the printer plugged in via OTG, `lsusb` must show it in the device
list. If it does NOT appear, USB passthrough is not available in this VM —
STOP HERE, do not implement the rest of this task, and report back that USB
printing isn't achievable in the Linux Terminal app on this device (the
options at that point are: stay on the LAN/tunnel path from Task 1 only, or
run this specific helper from Termux instead, which uses a different,
separately-supported Android USB access path).

If `lsusb` DOES show the printer, continue:
  ls -l /dev/usb/lp0
This should show a character device (created by the kernel's usblp driver).
If /dev/usb/lp0 doesn't exist even though lsusb sees the device, check
`dmesg | tail -30` after plugging in — the usblp driver may need
`sudo modprobe usblp`, or the printer may not be identifying as USB Printer
Class (bInterfaceClass 7) in which case this whole approach needs
revisiting; report back rather than guessing further.

Once /dev/usb/lp0 is confirmed to exist, continue with Step 1.

STEP 1 — Add to printer_helper.py (the same file from Task 1)

1a) New imports/config near the top:
```python
import glob

USB_ENABLED = os.getenv("USB_ENABLED", "true").lower() == "true"
USB_DETECT_INTERVAL = float(os.getenv("USB_DETECT_INTERVAL", "2"))
```

1b) New state (alongside the existing globals):
```python
usb_device_path: Optional[str] = None
active_print_path: str = "none"  # "usb" | "lan" | "none" — last path that actually succeeded
```

1c) New functions (place near send_to_printer / check_printer_reachable):
```python
def find_usb_printer() -> Optional[str]:
    """The kernel's usblp driver auto-binds any USB Printer Class device
    and exposes it as /dev/usb/lp0 (lp1, lp2... if more than one is ever
    attached). No libusb or permission dance needed — this is a real Linux
    kernel with a real driver, confirmed working in Step 0 above."""
    nodes = sorted(glob.glob("/dev/usb/lp*"))
    return nodes[0] if nodes else None


def usb_detect_loop():
    """Polls for attach/detach every USB_DETECT_INTERVAL seconds. (A
    udev-event-driven version is possible via pyudev for instant detection,
    but requires confirming the exact subsystem name usblp registers under
    on this kernel via `udevadm monitor --udev` — treat that as a later
    optimization, not a blocker; a couple seconds' detection delay is fine
    here.)"""
    global usb_device_path
    logger.info("USB detect loop started (interval %.1fs).", USB_DETECT_INTERVAL)
    while worker_running:
        found = find_usb_printer()
        with state_lock:
            if found != usb_device_path:
                logger.info(
                    "USB printer %s: %s",
                    "attached" if found else "detached",
                    found or usb_device_path,
                )
                usb_device_path = found
        time.sleep(USB_DETECT_INTERVAL)


def send_to_printer_usb(data: bytes, device_path: str) -> None:
    """Direct write to the usblp character device."""
    with open(device_path, "wb") as f:
        f.write(data)
        f.flush()


def send_to_printer_auto(data: bytes) -> str:
    """USB first if a device is currently detected, LAN otherwise or on any
    USB failure. Returns which path actually succeeded."""
    global active_print_path
    with state_lock:
        device_path = usb_device_path

    if USB_ENABLED and device_path:
        try:
            send_to_printer_usb(data, device_path)
            with state_lock:
                active_print_path = "usb"
            return "usb"
        except Exception as exc:
            logger.warning("USB print failed (%s) — falling back to LAN.", exc)

    send_to_printer(data)  # existing TCP function from Task 1, unchanged — raises on failure
    with state_lock:
        active_print_path = "lan"
    return "lan"
```

`process_job()` and `print_ticket()` in the Task 1 file already call
`send_to_printer_auto` — no further changes needed there once this is added.
`status()` in the Task 1 file already returns `usb_connected`,
`usb_device_path`, and `active_print_path` — no further changes needed
there either.

STEP 2 — One-time permission setup (manual, on the tablet)
The usblp device node is typically owned root:lp, mode 660 — the user
running printer_helper.py needs to be in the `lp` group rather than running
the whole service as root:
```
sudo usermod -aG lp $USER
```
Log out and back in (or restart the VM) for the group change to take
effect. Verify with:
```
ls -l /dev/usb/lp0     # should show group "lp"
groups                 # should include "lp" after relogin
```
If the device node's ownership/permissions look different on this system,
adjust via a udev rule instead (`udevadm info -a /dev/usb/lp0` to inspect
the actual subsystem/attributes first — don't guess the rule blind):
```
echo 'KERNEL=="lp[0-9]*", SUBSYSTEM=="usbmisc", MODE="0660", GROUP="lp"' | sudo tee /etc/udev/rules.d/99-usb-printer.rules
sudo udevadm control --reload-rules
```

STEP 3 — Test before relying on auto-detect
```
python3 -c "
import printer_helper as p
p.send_to_printer_usb(b'HELLO\n\n\n', p.find_usb_printer())
"
```
Confirm this actually prints before trusting the full auto-routing flow in
production.

ENV VARS ADDED (Task 2)
USB_ENABLED=true
USB_DETECT_INTERVAL=2

DO NOT CHANGE
Do not touch lib/print/print-bridge.ts or app/api/printer-status/route.ts —
both tasks are entirely inside printer_helper.py. The Next.js side already
just POSTs payload_base64 to /print-ticket and reads /status; it doesn't
need to know whether the helper printed over USB or LAN, that's already
surfaced via the usb_connected/active_print_path fields once Task 1's
/api/printer-status route is live.
```