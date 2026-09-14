# Label Bench — Brother QL-800 Label Printer

A clean web app to design, preview, and print labels on a **Brother QL-800**
(and other Brother QL-series printers) over USB. Runs in Docker, works from
any browser on your network.

- **Text labels** — unlimited rows with optional 5-digit unique codes, list / grid / boxed layouts
- **QR codes, barcodes, image upload, batch runs** with `{n}` templating
- **Unique codes in batch runs** — every label in a batch gets its own 5-digit code, generated automatically
- **Live preview** that never touches the printer
- **Resilient printing** — wake-from-sleep retries, automatic USB ↔ kernel-device fallback, clear error messages
- **Print history** with per-code print counts

## Quick start

**1. Install the udev rule** (on the Docker *host*, one time):

```bash
sudo cp 99-brother-ql800.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

This grants USB access and disables OS-level USB autosuspend for the QL-800
(the most common cause of "printer unreachable" flapping).

**2. Start the app:**

```bash
docker compose up --build -d
```

**3. Open it:** http://your-server:8013

Plug in the printer, press **Reconnect** in the sidebar if needed, then
Preview and Print.

## Wi-Fi printers (QL-810W / QL-820NWB)

> The QL-800 itself is USB-only. Wireless printing is for the Wi-Fi models
> (QL-810W, QL-820NWB, QL-1110NWB) over your LAN.

1. Connect the printer to your Wi-Fi (see Brother's manual; the Wi-Fi LED
   lights up when joined).
2. Find its IP address — check your router's client list, or print the
   printer's network configuration label.
3. Give it a **static IP** (or DHCP reservation) so the address never changes.
4. In the app, open the **Printer** panel → **Network (Wi-Fi)** → enter the
   IP (port `9100`) → pick the matching **model** → **Test connection** →
   **Save printer**.

No USB mapping or udev rule is needed for Wi-Fi printing, but the container
must be on the same network as the printer (the default bridge network is
fine as long as the host can reach the printer's IP). You can also set it
via environment instead: `PRINTER: "tcp://192.168.1.50:9100"`.

## Naming the printer

The name shown in the browser tab, sidebar, and status (default
**"Brother QL-800"**) is controlled by one setting:

```yaml
# docker-compose.yml
environment:
  PRINTER_DISPLAY_NAME: "Brother QL-800"
```

Change it to anything you like (e.g. `"Warehouse QL-800"`), then
`docker compose up -d` to apply. `PRINTER_MODEL` (`QL-800`) is the
technical model passed to the print driver — only change it if you switch
to a different QL-series printer.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `PRINTER` | `usb://0x04f9:0x209b` | Printer URI (`usb://…`, `file:///dev/usb/lp0`, `tcp://…`) |
| `PRINTER_MODEL` | `QL-800` | Driver model |
| `PRINTER_DISPLAY_NAME` | `Brother QL-800` | Friendly name shown in the UI |
| `DEFAULT_LABEL_SIZE` | `62` | Default label width (mm) |
| `PORT` | `8013` | Web UI port |
| `SERIAL_DB` | `/app/data/label_serials.db` | SQLite history database |
| `PRINT_MAX_RETRIES` | `5` | Print attempts per job |
| `PRINT_RETRY_DELAY` | `2.5` | Base retry delay (s, progressive backoff) |
| `PRINT_WAKE_WAIT` | `1.5` | Settle wait after waking a sleeping device |
| `PRINT_STATUS_TIMEOUT` | `10` | Seconds to wait for completion status |
| `PRINTER_FALLBACKS` | _(empty)_ | Extra URIs to try, e.g. `file:///dev/usb/lp0` |
| `PRINTER_AUTO_FALLBACK` | `1` | Auto-try discovered Brother USB / `/dev/usb/lp*` devices |

## Unique codes in batch runs

Batch runs can stamp a **unique 5-digit code** on every label, using the
same code pool, history, and validation as manually entered codes.

Pick how the code is placed with **Unique 5-digit code** in the Batch run
panel:

| Mode | Result |
|---|---|
| **Off** | No code — the template text only (default behaviour) |
| **In the template (`{code}`)** | The code replaces `{code}` wherever it appears, e.g. `ITEM-{n}-{code}` → `ITEM-7-42867` |
| **On its own line below the text** | The template text, with the code centred underneath (Text batches) |
| **Both** | `{code}` is replaced *and* the code prints on its own line |

Notes:

- `{n}` is the batch counter; `{code}` (alias `{serial}`) is the unique
  code. Both work anywhere in the template.
- Codes are generated automatically on **Preview** and **Print** — there is
  no separate button. The assigned codes are listed next to the preview
  (with a **Copy** button) and recorded in **Summary** with their print
  counts.
- Re-running the same batch reuses the same codes, so reprints come out
  identical. Where the code sits is a layout choice, not a different
  batch: moving `{code}` out of the template (or back in) keeps the same
  codes. To get a fresh set, delete the codes in **Summary**; deleted
  codes are replaced on the next run.
- Codes are reserved in one transaction, so a batch can never hand out a
  code twice or half-succeed. If the 5-digit pool runs out, nothing is
  reserved and the error tells you how many codes are left.
- A batch that wants an inline code but has no `{code}` in its template is
  rejected instead of silently printing uncoded labels.

## API overview

| Endpoint | Description |
|---|---|
| `GET /` | Web UI |
| `GET /api/health` | Liveness probe (Docker healthcheck) |
| `GET /api/status` | Live printer status + last print outcome |
| `GET /api/printer/diagnostics` | USB/kernel device visibility, config |
| `POST /api/printer/reconnect` | Wake / re-probe, optional `{"reset": true}` USB reset |
| `GET/POST /api/printer/config` | Printer connection settings (UI-overridable) |
| `POST /api/printer/test` | Probe a URI without saving it |
| `POST /api/preview` | Render previews (never touches the printer); batch runs return their assigned `batch_codes` |
| `POST /api/print` | Print labels; batch runs return and record their `batch_codes` |
| `POST /api/serial/*` | Unique-code generate / check / register |
| `GET /api/summary` | Print history |

## Troubleshooting

See **[PRINTER_TROUBLESHOOTING.md](PRINTER_TROUBLESHOOTING.md)** — it covers
the auto-off vs autosuspend distinction, the host checklist (`lsusb`,
permissions), every error message, and recovery steps. In the UI, the
**Diagnose** button shows the live device list.

## Development

```bash
pip install -r requirements.txt
SERIAL_DB=./data/label_serials.db PORT=8013 python app.py
```
