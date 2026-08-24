"""Kivy UI for the Parking Printer APK.

Minimal status screen. On launch it starts the background print service
(declared in buildozer.spec as `printer:printer_server.py`) and polls
localhost:8765/status to show live state. If the service API is unavailable
on a given p4a build, it falls back to running the server in-process.
"""

import json
import os
import threading
import urllib.request

from kivy.app import App
from kivy.clock import Clock
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label

SERVICE_NAME = "printer"
SERVER_URL = "http://127.0.0.1:8765"
SECRET = os.environ.get("PRINT_HELPER_SECRET", "")


class Root(BoxLayout):
    def __init__(self, **kw):
        super().__init__(orientation="vertical", **kw)
        self.status = Label(text="Starting service...", size_hint=(1, 0.8))
        self.add_widget(self.status)
        btn = Button(text="Open Status", size_hint=(1, 0.2))
        btn.bind(on_press=lambda *_: self.refresh())
        self.add_widget(btn)
        self.service_started = False
        Clock.schedule_once(self.start_service, 0.5)
        Clock.schedule_interval(self.refresh, 2)

    def start_service(self, *_):
        if self.service_started:
            return
        if self._reachable():
            self.service_started = True
            return
        started = False
        for starter in self._starters():
            try:
                starter()
                started = True
                break
            except Exception:
                continue
        if not started:
            # In-process fallback
            try:
                import printer_server
                threading.Thread(target=printer_server.run_server, daemon=True).start()
                started = True
            except Exception as e:
                self.status.text = f"Could not start print service:\n{e}"
        self.service_started = started

    @staticmethod
    def _starters():
        out = []
        try:
            from android import service as p4a_service
            out.append(lambda: p4a_service.start(SERVICE_NAME))
        except Exception:
            pass
        try:
            import android
            out.append(lambda: android.start_service(service_name=SERVICE_NAME))
        except Exception:
            pass
        return out

    @staticmethod
    def _reachable():
        try:
            req = urllib.request.Request(
                SERVER_URL + "/health", headers={"X-Print-Secret": SECRET}
            )
            with urllib.request.urlopen(req, timeout=2) as r:
                return r.status == 200
        except Exception:
            return False

    def refresh(self, *_):
        try:
            req = urllib.request.Request(
                SERVER_URL + "/status",
                headers={"X-Print-Secret": SECRET},
            )
            with urllib.request.urlopen(req, timeout=3) as r:
                d = json.loads(r.read())
            self.status.text = (
                f"Printer : {d['printer']}\n"
                f"Reachable: {d['printer_reachable']}\n"
                f"USB      : {d['usb_connected']} (path: {d['active_print_path']})\n"
                f"Processed: {d['jobs_processed']}  Failed: {d['jobs_failed']}\n"
                f"Last via : {d['last_printed_via']}\n"
                f"Version  : {d['version']}"
            )
        except Exception as e:
            self.status.text = f"Service not reachable yet:\n{e}"


class PrinterApp(App):
    def build(self):
        return Root()


if __name__ == "__main__":
    PrinterApp().run()
