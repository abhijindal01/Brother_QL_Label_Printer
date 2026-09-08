# QL-800 "Printer unreachable" troubleshooting

This app talks to the Brother QL-800 over raw USB (`usb://0x04f9:0x209b` via
the `pyusb` backend, with automatic fallback to `file:///dev/usb/lp*` via the
`linux_kernel` backend). The most common causes of flapping
"Printer unreachable" errors are OS-level, **not** the printer's own
auto-off setting.

## Auto-off (printer) vs autosuspend (OS) — the key distinction

- **Printer auto-off** is a QL-800 firmware setting. Disabling it keeps the
  printer powered, but it does **not** stop the host OS from suspending the
  USB port.
- **Linux USB autosuspend** powers down idle USB ports at the OS level. A
  suspended QL-800 stops answering descriptor reads for a second or two,
  which surfaces as `Device not found` / timeouts even though the printer
  looks "on" (green LED lit).

The app now handles this automatically where possible:

1. Every print attempt re-scans the USB bus (no stale handles).
2. From the 2nd attempt on, it sends a cheap descriptor read to **wake** an
   autosuspended device, then waits `PRINT_WAKE_WAIT` (default 1.5 s).
3. Retries use progressive backoff (`PRINT_RETRY_DELAY` × 1 → 1.5 → 2 …,
   capped at 8 s) so slow firmware wake-ups are tolerated.
4. If the primary URI stays unreachable, configured `PRINTER_FALLBACKS` and
   auto-discovered Brother devices / `/dev/usb/lp*` nodes are tried.
5. Each attempt opens the USB handle fresh and **always disposes it**
   (re-attaching the kernel driver when needed), so failures never leak a
   claimed interface.
6. Concurrent prints are serialised with a lock (`Another print job is
   already in progress` instead of `Resource busy`).

If errors persist after the app's retries, work through the checklist below.

## Quick recovery (in the UI)

1. Look at the sidebar status dot — it now reflects a **live USB probe**,
   not a static assumption. Hover/read the detail line under it.
2. Press **Diagnose** to see the live Brother USB device list, kernel
   `lp` nodes, fallback order, and the last print error.
3. Press **Reconnect** (wake + re-probe). If still missing, press
   **USB reset** once, wait ~5 s, then retry printing.
4. Still missing? `Unplug 10 s → replug → wait 5 s → Reconnect → Print`
   recovers nearly all transient states.

API equivalents: `GET /api/status`, `GET /api/printer/diagnostics`,
`POST /api/printer/reconnect {"reset": false|true}`.

## Host checklist (run on the Docker host, not in the container)

```bash
# 1. Is the printer on the bus at all? No match = cable/power/host issue,
#    the container cannot help until this shows the printer.
lsusb | grep 04f9
# expected: Bus 001 Device 0NN: ID 04f9:209b Brother Industries, Ltd QL-800 ...

# 2. Permissions / nodes visible?
ls -la /dev/bus/usb/*/* | grep -i brother   # or just inspect the bus
ls -la /dev/usb/lp* 2>/dev/null

# 3. Kernel messages when (re)plugging:
dmesg | tail -20
# look for "new full-speed USB device", NOT "device descriptor read/64,
# error -71" (cable/power) or "unable to enumerate" (hub/power).
```

## One-time host setup

1. Install the udev rule (disables autosuspend for the QL-800 too):

   ```bash
   sudo cp 99-brother-ql800.rules /etc/udev/rules.d/
   sudo udevadm control --reload-rules && sudo udevadm trigger
   # unplug 10 s, replug, retry
   ```

2. Keep the compose USB mapping and privileged mode:

   ```yaml
   devices:
     - "/dev/bus/usb:/dev/bus/usb"
   privileged: true
   ```

   `privileged` is required for `detach_kernel_driver()` (needs
   `CAP_SYS_ADMIN`); the udev rule alone only fixes file permissions.

3. If you use `PRINTER=file:///dev/usb/lp0`, also map that node and set
   `PRINTER_FALLBACKS: "usb://0x04f9:0x209b"` (or vice versa) so either
   backend can take over.

## Error message guide

| Message fragment | Meaning | Action |
|---|---|---|
| `Device not found` | Printer not on the bus during the scan | Cable/power, `lsusb`, replug |
| `No backend available` / `libusb` | libusb missing in container | Rebuild image (Dockerfile installs `libusb-1.0-0`), map `/dev/bus/usb` |
| `Access denied` / `Permission` | Udev/permissions | Install udev rule, keep `privileged: true`, replug |
| `Resource busy` / `busy` | Held by usblp/another job | Wait + retry; jobs are serialised, usually clears alone |
| `Timeout` / `timed out` | Slow/unstable USB link | Different port/cable, no hubs, disable autosuspend (udev rule does this) |
| `Cover opened` / `No media` / `End of media` | Printer state, fails fast (no blind retries) | Reload labels, close cover, check label size |
| `Transmission / Communication error` | Transient transfer glitch | Retried automatically |
| `Another print job is already in progress` | Concurrent print | Wait a few seconds |
| `Label data was sent but the printer did not confirm` | Bytes accepted, status reply incomplete | Check output; if the label printed, ignore |

## Tuning knobs (env vars)

| Var | Default | Effect |
|---|---|---|
| `PRINT_MAX_RETRIES` | `5` | Attempts on the primary URI |
| `PRINT_RETRY_DELAY` | `2.5` | Base delay (s), ×1 → ×1.5 → ×2 … capped 8 s |
| `PRINT_WAKE_WAIT` | `1.5` | Settle wait after waking an autosuspended device |
| `PRINT_STATUS_TIMEOUT` | `10` | Seconds to wait for completion status |
| `PRINTER_FALLBACKS` | _(empty)_ | Comma-separated extra URIs, e.g. `file:///dev/usb/lp0` |
| `PRINTER_AUTO_FALLBACK` | `1` | Auto-try discovered Brother USB + `/dev/usb/lp*` devices |

## Verifying inside the container

```bash
docker compose exec label-bench lsusb | grep 04f9
docker compose exec label-bench ls -la /dev/usb/
curl -s localhost:8013/api/printer/diagnostics | python3 -m json.tool | head -60
docker compose logs label-bench | grep print_images | tail -20
```
