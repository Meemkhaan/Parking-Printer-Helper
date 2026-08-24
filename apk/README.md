# Parking Printer — Android APK

Builds the printer helper as a standalone Android app (no Linux VM, no
Termux, no Android Studio needed on your machine). CI builds the APK with
GitHub Actions and uploads it as a downloadable artifact.

Same API contract as the Linux-VM helper, so the Next.js frontend is
unchanged: it still probes `http://localhost:8765` → `/print-ticket`.

```
Browser tab → http://localhost:8765/print-ticket → [APK service]
                                                      ├─ USB (Android UsbManager, class-7 auto-detect)
                                                      └─ LAN TCP PRINTER_IP:9100
```

## Files

| File | Purpose |
|---|---|
| `usb_bridge.py` | pyjnius bridge to Android `UsbManager` (find + permission + bulk write) |
| `printer_server.py` | Background service: stdlib HTTP server on 8765 + print routing |
| `main.py` | Kivy status screen; starts the service, polls `/status` |
| `buildozer.spec` | python-for-android packaging config |
| `.github/workflows/build-apk.yml` | Builds + uploads the signed debug APK |

## Build (zero local tooling)

1. Push a tag: `git tag v1.0.0 && git push origin v1.0.0`
   (or run **Actions → Build APK → Run workflow**).
2. Download `parking-printer-apk` from the run's artifacts.

## Install on the tablet

1. Copy `bin/*.apk` to the tablet, open it; allow "install from unknown sources".
2. Launch **Parking Printer**.
3. On first launch with a USB printer attached, Android shows the USB
   permission dialog — tap **Allow** (and "allow always" if offered).

## Configure

The helper reads env vars at service start. To set `PRINTER_IP` /
`PRINT_HELPER_SECRET` per tablet, rebuild with edited values in
`buildozer.spec` (`[app]` `android.additional_environment`? p4a uses
`android.putExtra` at runtime — simplest is to bake values as defaults in
`printer_server.py` or pass via a small config file the service reads).
`PRINT_HELPER_SECRET` **must** match the value in the Next.js app
(`NEXT_PUBLIC_PRINT_HELPER_SECRET`).

## Keep it alive

- The service runs a persistent foreground notification ("Printing service
  active"); do not swipe it away.
- Disable battery optimization for the app (Android settings, or it will be
  killed when backgrounded).
- For a fixed kiosk, use Android's "Pin app" / screen-pinning so staff can't
  close it.

## Verify

```bash
# From any device with the APK installed (adb shell / Termux):
curl http://127.0.0.1:8765/health
# {"status":"online"}

curl -X POST http://127.0.0.1:8765/print \
  -H "X-Print-Secret: <SECRET>" \
  -H "Content-Type: application/json" \
  -d '{"ticket":"T-TEST-001","vehicle":"Car","number":"ABC-123","amount":50}'
```

## Known caveats (test on-device before rollout)

- **Android 14+ foreground service type:** if the service crashes on start,
  the manifest needs `android:foregroundServiceType="dataSync"` on the
  generated service. Add a custom `AndroidManifest.xml` template via
  `android.manifest_template` in `buildozer.spec` if required.
- **USB permission** is per-device; the first-run dialog must be granted or
  printing silently falls back to LAN.
- **Mixed-content:** the app is served over HTTPS while the helper is HTTP —
  browsers exempt `localhost`/`127.0.0.1` from mixed-content blocking, so
  this is fine on the tablet itself.
- `pydantic`/`FastAPI` are **not** used inside the APK (they don't build for
  Android); the server is pure stdlib, intentionally mirroring the v4
  contract.
