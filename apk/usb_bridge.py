"""USB bridge for Android thermal printers.

Uses Android's UsbManager via pyjnius (no /dev/usb/lp0 on Android).
Auto-detects USB Printer Class (class 7) devices. First run shows the
standard Android permission dialog; once granted, subsequent writes work
without re-prompting. LAN printing is handled separately by the server.
"""

import threading

try:
    from jnius import autoclass
    _HAS_JNIUS = True
except ImportError:
    _HAS_JNIUS = False

USB_CLASS_PRINTER = 7
USB_ENDPOINT_XFER_BULK = 2
USB_DIR_OUT = 0

_context = None
_usb_manager = None
_permission_intent = None
_lock = threading.Lock()


def init_android():
    """Initialise the UsbManager + permission intent. Safe to call repeatedly."""
    global _context, _usb_manager, _permission_intent

    if not _HAS_JNIUS:
        return False
    if _usb_manager is not None:
        return True

    try:
        Context = autoclass("android.content.Context")
        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        PythonService = autoclass("org.kivy.android.PythonService")

        try:
            _context = PythonActivity.mActivity
        except Exception:
            _context = PythonService.mService
        if _context is None:
            return False

        _usb_manager = _context.getSystemService(Context.USB_SERVICE)

        Intent = autoclass("android.content.Intent")
        PendingIntent = autoclass("android.app.PendingIntent")
        _permission_intent = PendingIntent.getActivity(
            _context,
            0,
            Intent("com.reachsolutions.parkingprinter.USB_PERMISSION"),
            0x02000000 if hasattr(PendingIntent, "FLAG_MUTABLE") else 0,  # FLAG_MUTABLE
        )
        return _usb_manager is not None
    except Exception as e:
        print("usb_bridge init failed:", e)
        return False


def _is_printer(device):
    for i in range(device.getInterfaceCount()):
        iface = device.getInterface(i)
        if iface.getInterfaceClass() == USB_CLASS_PRINTER:
            return True
    return False


def find_printer():
    """Return the first connected USB Printer Class device, or None."""
    if _usb_manager is None:
        return None
    try:
        devs = _usb_manager.getDeviceList()
        it = devs.values().iterator()
        while it.hasNext():
            d = it.next()
            if _is_printer(d):
                return d
    except Exception as e:
        print("find_printer error:", e)
    return None


def ensure_permission(device):
    """Request permission if needed. Returns True only if already granted."""
    if _usb_manager.hasPermission(device):
        return True
    try:
        if _permission_intent is not None:
            _usb_manager.requestPermission(device, _permission_intent)
    except Exception as e:
        print("requestPermission error:", e)
    return False


def _find_bulk_out(device):
    for i in range(device.getInterfaceCount()):
        iface = device.getInterface(i)
        if iface.getInterfaceClass() == USB_CLASS_PRINTER:
            for j in range(iface.getEndpointCount()):
                ep = iface.getEndpoint(j)
                if ep.getType() == USB_ENDPOINT_XFER_BULK and (ep.getDirection() & 0x80) == USB_DIR_OUT:
                    return iface, ep
    return None, None


def write_bytes(device, data):
    """Open the device, claim the printer interface, bulk-transfer out."""
    conn = _usb_manager.openDevice(device)
    if conn is None:
        return False
    try:
        iface, ep = _find_bulk_out(device)
        if iface is None or ep is None:
            return False
        conn.claimInterface(iface, True)
        try:
            res = conn.bulkTransfer(ep, bytes(data), len(data), 5000)
        finally:
            conn.releaseInterface(iface)
        return res >= 0
    except Exception as e:
        print("usb write error:", e)
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def send_via_usb(data):
    """USB-first attempt. Returns True on successful write."""
    dev = find_printer()
    if dev is None:
        return False
    if not ensure_permission(dev):
        return False
    if not _usb_manager.hasPermission(dev):
        # permission dialog shown; will succeed on a later cycle
        return False
    return write_bytes(dev, data)


def usb_connected():
    return find_printer() is not None
