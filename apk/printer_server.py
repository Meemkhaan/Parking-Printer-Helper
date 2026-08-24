#!/usr/bin/env python3
"""Parking Printer Helper — Android background service (v4.0.0-apk).

Hosts the same HTTP API as the Linux-VM helper (localhost:8765) so the Next.js
frontend contract is unchanged. Routing is USB-first via Android UsbManager,
falling back to LAN/TCP. Pure stdlib HTTP server (no FastAPI/pydantic) so it
builds cleanly for Android with python-for-android.

Run either:
  * directly (python printer_server.py) for testing on a PC, or
  * as a python-for-android service (declared in buildozer.spec).
"""

import base64
import json
import hmac
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import usb_bridge

PRINTER_IP = os.environ.get("PRINTER_IP", "192.168.18.100")
PRINTER_PORT = int(os.environ.get("PRINTER_PORT", "9100"))
PRINT_HELPER_SECRET = os.environ.get("PRINT_HELPER_SECRET", "")
PRINTER_TIMEOUT = float(os.environ.get("PRINTER_TIMEOUT", "5"))
PRINTER_PROBE_TIMEOUT = float(os.environ.get("PRINTER_PROBE_TIMEOUT", "1.5"))

state = {
    "jobs_processed": 0,
    "jobs_failed": 0,
    "last_error": None,
    "last_printed_job_id": None,
    "last_printed_via": "none",
    "usb_connected": False,
    "active_print_path": "none",
    "started_at": time.time(),
}
_state_lock = threading.Lock()


# ============================================================
# Printer paths
# ============================================================

def send_to_printer_lan(data):
    if not data:
        raise ValueError("Print payload is empty.")
    with socket.create_connection((PRINTER_IP, PRINTER_PORT), timeout=PRINTER_TIMEOUT) as s:
        s.sendall(data)
        try:
            s.shutdown(socket.SHUT_WR)
        except Exception:
            pass
        time.sleep(0.2)


def check_printer_reachable(timeout=PRINTER_PROBE_TIMEOUT):
    try:
        with socket.create_connection((PRINTER_IP, PRINTER_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def send_to_printer_auto(data):
    """USB first, LAN fallback. Returns which path succeeded."""
    if usb_bridge.init_android() and usb_bridge.send_via_usb(data):
        with _state_lock:
            state["active_print_path"] = "usb"
            state["usb_connected"] = True
        return "usb"

    send_to_printer_lan(data)  # raises on failure
    with _state_lock:
        state["active_print_path"] = "lan"
        state["usb_connected"] = usb_bridge.usb_connected()
    return "lan"


def build_test_receipt(job):
    ESC = b"\x1b"
    GS = b"\x1d"
    data = bytearray()
    data += ESC + b"@"
    data += ESC + b"a" + b"\x01"
    data += b"PARKING RECEIPT\n"
    data += b"--------------------------------\n"
    data += ESC + b"a" + b"\x00"
    data += f"Ticket : {job.get('ticket','')}\n".encode()
    data += f"Vehicle: {job.get('vehicle','')}\n".encode()
    data += f"Number : {job.get('number','')}\n".encode()
    data += f"Amount : Rs. {float(job.get('amount', 0)):.2f}\n".encode()
    data += b"--------------------------------\n"
    data += ESC + b"a" + b"\x01"
    data += b"Thank You\n"
    data += b"\n\n\n"
    data += GS + b"V" + b"\x00"
    return bytes(data)


# ============================================================
# HTTP handler
# ============================================================

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # quiet

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Print-Secret")

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _secret_ok(self):
        return (
            PRINT_HELPER_SECRET != ""
            and hmac.compare_digest(self.headers.get("X-Print-Secret", ""), PRINT_HELPER_SECRET)
        )

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"status": "online"})
            return
        if self.path == "/status":
            if not self._secret_ok():
                self._send_json(401, {"detail": "Invalid or missing X-Print-Secret"})
                return
            with _state_lock:
                s = dict(state)
            self._send_json(200, {
                "status": "online",
                "version": "4.0.0-apk",
                "uptime_seconds": int(time.time() - s["started_at"]),
                "printer": PRINTER_IP,
                "printer_port": PRINTER_PORT,
                "printer_reachable": check_printer_reachable(),
                "usb_connected": s["usb_connected"],
                "active_print_path": s["active_print_path"],
                "last_printed_job_id": s["last_printed_job_id"],
                "last_printed_via": s["last_printed_via"],
                "last_error": s["last_error"],
                "jobs_processed": s["jobs_processed"],
                "jobs_failed": s["jobs_failed"],
            })
            return
        self._send_json(404, {"detail": "not found"})

    def do_POST(self):
        if self.path not in ("/print-ticket", "/print"):
            self._send_json(404, {"detail": "not found"})
            return
        if not self._secret_ok():
            self._send_json(401, {"detail": "Invalid or missing X-Print-Secret"})
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except Exception:
            self._send_json(400, {"detail": "Invalid JSON"})
            return

        if self.path == "/print-ticket":
            b64 = payload.get("payload_base64", "")
            try:
                data = base64.b64decode(b64, validate=True)
            except Exception as e:
                self._send_json(400, {"detail": f"Invalid base64 payload: {e}"})
                return
            if not data:
                self._send_json(400, {"detail": "Decoded to zero bytes"})
                return
            try:
                path = send_to_printer_auto(data)
            except Exception as e:
                msg = str(e) or "Print failed."
                with _state_lock:
                    state["last_error"] = msg
                    state["jobs_failed"] += 1
                self._send_json(503, {"detail": msg})
                return
            with _state_lock:
                state["jobs_processed"] += 1
                state["last_error"] = None
                state["last_printed_job_id"] = payload.get("ticket_number") or state["last_printed_job_id"]
                state["last_printed_via"] = path
            self._send_json(200, {"success": True, "path": path})
            return

        # /print — local test receipt (LAN only)
        try:
            receipt = build_test_receipt(payload)
            send_to_printer_lan(receipt)
        except Exception as e:
            msg = str(e) or "Print failed."
            with _state_lock:
                state["last_error"] = msg
                state["jobs_failed"] += 1
            self._send_json(500, {"detail": msg})
            return
        self._send_json(200, {"success": True, "message": "Receipt sent to printer"})


# ============================================================
# Foreground notification (keeps the service alive)
# ============================================================

def setup_foreground_notification():
    if not usb_bridge._HAS_JNIUS:
        return
    try:
        from jnius import autoclass
        PythonService = autoclass("org.kivy.android.PythonService")
        service = PythonService.mService
        if service is None:
            return

        Context = autoclass("android.content.Context")
        NotificationManager = autoclass("android.app.NotificationManager")
        Notification = autoclass("android.app.Notification")
        NotificationChannel = autoclass("android.app.NotificationChannel")

        nm = service.getSystemService(Context.NOTIFICATION_SERVICE)
        chan = "parking_printer"
        try:
            channel = NotificationChannel(chan, "Parking Printer", NotificationManager.IMPORTANCE_LOW)
            nm.createNotificationChannel(channel)
        except Exception:
            pass

        try:
            R_drawable = autoclass("android.R$drawable")
            icon = R_drawable.ic_dialog_info
        except Exception:
            icon = 0

        builder = Notification.Builder(service, chan)
        builder.setContentTitle("Parking Printer")
        builder.setContentText("Printing service active")
        builder.setSmallIcon(icon)
        builder.setOngoing(True)
        service.startForeground(1, builder.build())
    except Exception as e:
        print("foreground notification setup failed:", e)


# ============================================================
# Entry point
# ============================================================

def run_server(host="0.0.0.0", port=8765):
    server = ThreadingHTTPServer((host, port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    usb_bridge.init_android()
    setup_foreground_notification()
    run_server()
