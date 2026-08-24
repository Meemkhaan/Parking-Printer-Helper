[app]

title = Parking Printer
package.name = parkingprinter
package.domain = com.reachsolutions
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,json,txt
version = 1.0.0
version.code = 1

requirements = python3==3.11.6, kivy, pyjnius

orientation = portrait
android.permissions = INTERNET, FOREGROUND_SERVICE, FOREGROUND_SERVICE_DATA_SYNC, WAKE_LOCK, REQUEST_IGNORE_BATTERY_OPTIMIZATIONS
android.features = android.hardware.usb.host
android.api = 34
android.minapi = 26
android.ndk = 25b
android.archs = arm64-v8a, armeabi-v7a
android.allow_backup = False
android.private_storage = True

# Background service running the print HTTP server
services = printer:printer_server.py

p4a.bootstrap = sdl2
p4a.branch = master

[buildozer]
log_level = 2
warn_on_root = 1
