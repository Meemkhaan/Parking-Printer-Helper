import base64
import glob
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

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


# ============================================================
# Configuration
# ============================================================

load_dotenv()

PRINTER_IP = os.getenv("PRINTER_IP", "192.168.18.100")
PRINTER_PORT = int(os.getenv("PRINTER_PORT", "9100"))

PRINT_HELPER_SECRET = os.getenv("PRINT_HELPER_SECRET", "")

PRINTER_TIMEOUT = float(os.getenv("PRINTER_TIMEOUT", "5"))
PRINTER_PROBE_TIMEOUT = float(os.getenv("PRINTER_PROBE_TIMEOUT", "1.5"))

USB_ENABLED = os.getenv("USB_ENABLED", "true").lower() == "true"
USB_DETECT_INTERVAL = float(os.getenv("USB_DETECT_INTERVAL", "2"))


# ============================================================
# Logging
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("parking-printer")


# ============================================================
# Validation
# ============================================================

if not PRINT_HELPER_SECRET:
    logger.warning(
        "PRINT_HELPER_SECRET is not set — protected endpoints "
        "will refuse all requests."
    )


# ============================================================
# Runtime state
# ============================================================

worker_running = True
started_at = datetime.now(timezone.utc)

last_error: Optional[str] = None
last_printed_job_id: Optional[str] = None
last_printed_via: str = "none"  # "usb" | "lan" | "none"
jobs_processed: int = 0
jobs_failed: int = 0

usb_device_path: Optional[str] = None
active_print_path: str = "none"  # alias of last_printed_via for /status

state_lock = threading.Lock()


# ============================================================
# Graceful shutdown
# ============================================================

def _shutdown_handler(signum, frame):
    global worker_running
    logger.info("Received signal %d — shutting down.", signum)
    worker_running = False

signal.signal(signal.SIGTERM, _shutdown_handler)
signal.signal(signal.SIGINT, _shutdown_handler)


# ============================================================
# Request models
# ============================================================

class PrintJobRequest(BaseModel):
    ticket: str
    vehicle: str
    number: str
    amount: float


class PrintTicketRequest(BaseModel):
    payload_base64: str
    ticket_number: Optional[str] = None


# ============================================================
# Auth
# ============================================================

def require_secret(x_print_secret: str = Header(default="")):
    if (
        not PRINT_HELPER_SECRET
        or not hmac.compare_digest(x_print_secret, PRINT_HELPER_SECRET)
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing X-Print-Secret",
        )


# ============================================================
# Printer — LAN/TCP path
# ============================================================

def send_to_printer(data: bytes):
    """Send raw ESC/POS bytes to the printer over TCP port 9100."""

    if not data:
        raise ValueError("Print payload is empty.")

    logger.info(
        "Sending %d bytes to printer %s:%d",
        len(data),
        PRINTER_IP,
        PRINTER_PORT,
    )

    with socket.create_connection(
        (PRINTER_IP, PRINTER_PORT),
        timeout=PRINTER_TIMEOUT,
    ) as printer:

        printer.sendall(data)

        try:
            printer.shutdown(socket.SHUT_WR)
        except Exception:
            pass

        time.sleep(0.2)

    logger.info("Printer accepted %d bytes.", len(data))


def check_printer_reachable(timeout: float = PRINTER_PROBE_TIMEOUT) -> bool:
    """Actually open a socket to the printer — real connectivity check."""

    try:
        with socket.create_connection(
            (PRINTER_IP, PRINTER_PORT),
            timeout=timeout,
        ):
            return True
    except OSError:
        return False


# ============================================================
# Printer — USB path
# ============================================================

def find_usb_printer() -> Optional[str]:
    """The kernel's usblp driver auto-binds any USB Printer Class device
    and exposes it as /dev/usb/lp0 (lp1, lp2... if more than one is ever
    attached). Requires real USB passthrough into this VM."""

    nodes = sorted(glob.glob("/dev/usb/lp*"))
    return nodes[0] if nodes else None


def usb_detect_loop():
    """Polls for attach/detach every USB_DETECT_INTERVAL seconds.
    Detection delay of a couple of seconds is acceptable here; a
    udev-event-driven version would be a later optimization."""

    global usb_device_path

    logger.info(
        "USB detect loop started (interval %.1fs).",
        USB_DETECT_INTERVAL,
    )

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
    """USB first if a device is currently detected, LAN otherwise or on
    any USB failure. Returns which path actually succeeded."""

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
            logger.warning(
                "USB print failed (%s) — falling back to LAN.",
                exc,
            )

    send_to_printer(data)  # raises on failure

    with state_lock:
        active_print_path = "lan"

    return "lan"


# ============================================================
# Local test receipt
# ============================================================

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


# ============================================================
# FastAPI lifespan
# ============================================================

_usb_thread: Optional[threading.Thread] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global worker_running, _usb_thread, started_at

    worker_running = True
    started_at = datetime.now(timezone.utc)

    if USB_ENABLED:
        _usb_thread = threading.Thread(
            target=usb_detect_loop,
            name="usb-detect",
            daemon=True,
        )
        _usb_thread.start()
        logger.info("Print helper ready. USB detect thread started.")
    else:
        logger.info("Print helper ready.")

    yield

    worker_running = False
    if _usb_thread:
        _usb_thread.join(timeout=5)

    logger.info("Print helper stopped.")


# ============================================================
# FastAPI app
# ============================================================

app = FastAPI(
    title="Parking Printer Helper",
    version="4.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# API endpoints
# ============================================================

@app.get("/health")
def health():
    """Liveness probe only — never touches the printer."""
    return {"status": "online"}


@app.get("/status", dependencies=[Depends(require_secret)])
def status():

    with state_lock:
        error = last_error
        printed = last_printed_job_id
        via = last_printed_via
        processed = jobs_processed
        failed = jobs_failed
        usb_path = usb_device_path
        active_path = active_print_path

    uptime_s = (datetime.now(timezone.utc) - started_at).total_seconds()

    return {
        "status": "online",
        "version": "4.0.0",
        "uptime_seconds": round(uptime_s),
        "printer": PRINTER_IP,
        "printer_port": PRINTER_PORT,
        "printer_reachable": check_printer_reachable(),
        "usb_connected": usb_path is not None,
        "usb_device_path": usb_path,
        "active_print_path": active_path,
        "last_printed_job_id": printed,
        "last_printed_via": via,
        "last_error": error,
        "jobs_processed": processed,
        "jobs_failed": failed,
    }


@app.post("/print-ticket", dependencies=[Depends(require_secret)])
def print_ticket(job: PrintTicketRequest):
    """Primary print path — called directly by the browser on this tablet.
    No queue, no Supabase: success/failure is returned immediately and the
    caller is responsible for recording the outcome in print_jobs."""

    global jobs_processed, jobs_failed, last_printed_job_id, last_error, last_printed_via

    try:
        print_bytes = base64.b64decode(job.payload_base64, validate=True)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid base64 payload: {exc}",
        )

    if not print_bytes:
        raise HTTPException(status_code=400, detail="Decoded to zero bytes")

    try:
        used = send_to_printer_auto(print_bytes)
    except Exception as exc:
        detail = str(exc) or "Print failed."
        logger.exception(
            "Direct print failed for %s",
            job.ticket_number or "(unknown)",
        )
        with state_lock:
            last_error = detail
            jobs_failed += 1
        raise HTTPException(status_code=503, detail=detail)

    with state_lock:
        jobs_processed += 1
        last_error = None
        last_printed_via = used
        last_printed_job_id = job.ticket_number or last_printed_job_id

    logger.info(
        "Printed %s via %s.",
        job.ticket_number or "(unknown)",
        used,
    )
    return {"success": True, "path": used}


@app.post("/print", dependencies=[Depends(require_secret)])
def print_local(job: PrintJobRequest):
    """Local test receipt generator — handy for install verification."""

    global jobs_processed, last_printed_job_id, last_error, last_printed_via

    try:

        receipt = create_test_receipt(job)

        send_to_printer(receipt)

        with state_lock:
            jobs_processed += 1
            last_error = None
            last_printed_via = "lan"

        return {
            "success": True,
            "message": "Receipt sent to printer",
        }

    except Exception as exc:

        with state_lock:
            last_error = str(exc) or "Print failed."
            jobs_failed += 1

        logger.exception("Local print failed.")

        raise HTTPException(
            status_code=500,
            detail=str(exc),
        )
