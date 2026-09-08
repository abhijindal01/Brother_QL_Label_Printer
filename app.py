import os
import io
import base64
import glob
import sqlite3
import random
import string
import traceback
import re
import logging
import threading
import socket
from datetime import datetime, timezone

from flask import Flask, request, jsonify, render_template, render_template_string
from PIL import Image, ImageDraw, ImageFont
import qrcode
import barcode
from barcode.writer import ImageWriter

import time
import usb.core
import usb.util

from brother_ql.raster import BrotherQLRaster
from brother_ql.conversion import convert
from brother_ql.backends.helpers import send as ql_send, guess_backend
from brother_ql.backends import backend_factory
from brother_ql.reader import interpret_response

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("label-bench")


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


PRINT_MAX_RETRIES = max(1, _env_int("PRINT_MAX_RETRIES", 5))
PRINT_RETRY_DELAY = max(0.2, _env_float("PRINT_RETRY_DELAY", 2.5))  # seconds
# Extra settle time after (re-)discovering the USB device, so a printer
# waking from USB autosuspend / sleep has time to answer.
PRINT_WAKE_WAIT = max(0.0, _env_float("PRINT_WAKE_WAIT", 1.5))  # seconds
# How long to wait for the printer's "Printing completed" + "Waiting to
# receive" status replies before treating the job as uncertain.
PRINT_STATUS_TIMEOUT = max(2.0, _env_float("PRINT_STATUS_TIMEOUT", 10.0))
# Optional comma-separated fallback printer URIs, tried after the primary
# PRINTER URI has exhausted its retries.
PRINTER_FALLBACKS = [
    s.strip()
    for s in os.environ.get("PRINTER_FALLBACKS", "").split(",")
    if s.strip()
]
# When true (default), automatically fall back between the pyusb
# (usb://...) and linux-kernel (file:///dev/usb/lp*) backends when the
# configured URI is unreachable. This handles hosts where the printer is
# visible on only one of the two interfaces.
PRINTER_AUTO_FALLBACK = str(
    os.environ.get("PRINTER_AUTO_FALLBACK", "1")
).lower() in {"1", "true", "yes", "on"}

app = Flask(__name__)

APP_NAME = "Label Bench"
APP_VERSION = "1.2.0"

MODEL = os.environ.get("PRINTER_MODEL", "QL-800")

# Friendly printer name shown throughout the UI (sidebar, page title,
# status). Set PRINTER_DISPLAY_NAME to rename it, e.g. "Brother QL-800".
PRINTER_DISPLAY_NAME = os.environ.get(
    "PRINTER_DISPLAY_NAME", ""
).strip() or f"Brother {MODEL}"

PRINTER = os.environ.get("PRINTER", "usb://0x04f9:0x209b")

DEFAULT_LABEL_SIZE = os.environ.get("DEFAULT_LABEL_SIZE", "62")
SERIAL_DB = os.environ.get("SERIAL_DB", "/app/data/label_serials.db")

# Brother QL models the app can drive. The QL-800 itself is USB-only;
# the W/NW models add Wi-Fi (and print via tcp://HOST:9100).
PRINTER_MODELS = [
    {"id": "QL-800", "label": "QL-800 (USB)", "wireless": False},
    {"id": "QL-810W", "label": "QL-810W (USB / Wi-Fi)", "wireless": True},
    {"id": "QL-820NWB", "label": "QL-820NWB (USB / Wi-Fi / Bluetooth)", "wireless": True},
    {"id": "QL-1100", "label": "QL-1100 (USB)", "wireless": False},
    {"id": "QL-1110NWB", "label": "QL-1110NWB (USB / Wi-Fi / Bluetooth)", "wireless": True},
]
PRINTER_MODEL_IDS = {m["id"] for m in PRINTER_MODELS}

# Default raw-socket (JetDirect/AppSocket) port Brother Wi-Fi printers
# listen on. This is what brother_ql's network backend speaks.
NETWORK_PRINTER_PORT = 9100
# Timeout for TCP connectivity probes (status page, Test button).
NETWORK_PROBE_TIMEOUT = max(
    1.0, _env_float("NETWORK_PROBE_TIMEOUT", 3.0)
)

# Serialize all USB access: concurrent Flask requests must never talk to
# the printer at the same time, otherwise libusb returns "Resource busy"
# / timeout errors and the kernel driver attach/detach bookkeeping races.
_PRINT_LOCK = threading.Lock()

# Last printer outcome, surfaced via /api/status so the UI can show *why*
# the printer is (un)reachable instead of just a green/red dot.
_PRINTER_STATE = {
    "last_error": None,
    "last_error_at": None,
    "last_success_at": None,
    "consecutive_failures": 0,
    "last_printer_used": None,
}

LABEL_SPECS = {
    "12": (106, None),
    "17x54": (165, 566),
    "17x87": (165, 956),
    "23x23": (202, 202),
    "29": (306, None),
    "29x42": (306, 425),
    "29x90": (306, 991),
    "38": (413, None),
    "39x48": (413, 495),
    "39x90": (413, 991),
    "50": (554, None),
    "52x29": (578, 306),
    "54": (590, None),
    "62": (696, None),
    "62x29": (696, 306),
    "62x100": (696, 1109),
    "102": (1164, None),
    "102x51": (1164, 526),
    "102x152": (1164, 1660),
}

FONT_DIR_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/truetype",
    "/usr/share/fonts/opentype",
    "/usr/share/fonts/type1",
]
FALLBACK_FONT = ImageFont.load_default()


def init_db():
    directory = os.path.dirname(SERIAL_DB)
    if directory:
        os.makedirs(directory, exist_ok=True)

    with sqlite3.connect(SERIAL_DB) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS used_serials (
                serial TEXT PRIMARY KEY,
                text_content TEXT,
                created_at TEXT NOT NULL,
                printed_at TEXT,
                print_count INTEGER NOT NULL DEFAULT 0
            )
        """)

        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(used_serials)")
        }

        if "text_content" not in columns:
            conn.execute(
                "ALTER TABLE used_serials ADD COLUMN text_content TEXT"
            )

        # Key/value store for UI-configurable settings. The printer
        # connection (URI / model / display name) chosen in the Printer
        # panel is persisted here and overrides the env-var defaults.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)


def get_setting(key, default=None):
    """Read one persisted setting; env/DB failures fall back to default."""
    try:
        init_db()
        with sqlite3.connect(SERIAL_DB) as conn:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key=?",
                (key,),
            ).fetchone()
    except Exception:
        return default
    return row[0] if row else default


def set_setting(key, value):
    init_db()
    with sqlite3.connect(SERIAL_DB) as conn:
        conn.execute(
            "INSERT INTO app_settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def delete_setting(key):
    try:
        init_db()
        with sqlite3.connect(SERIAL_DB) as conn:
            conn.execute("DELETE FROM app_settings WHERE key=?", (key,))
    except Exception:
        pass


def active_printer_uri():
    """Effective printer URI: UI setting wins, else the PRINTER env var."""
    saved = (get_setting("printer_uri", "") or "").strip()
    return saved or PRINTER


def active_model():
    """Effective driver model: UI setting wins, else PRINTER_MODEL env."""
    saved = (get_setting("printer_model", "") or "").strip()
    if saved in PRINTER_MODEL_IDS:
        return saved
    return MODEL if MODEL in PRINTER_MODEL_IDS else "QL-800"


def active_display_name():
    """Effective friendly printer name shown across the UI."""
    saved = (get_setting("printer_display_name", "") or "").strip()
    if saved:
        return saved
    if PRINTER_DISPLAY_NAME:
        return PRINTER_DISPLAY_NAME
    return f"Brother {active_model()}"


def connection_type_of(uri):
    uri = str(uri or "").strip()
    if uri.startswith("tcp://"):
        return "network"
    if uri.startswith("file://") or uri.startswith("/dev/"):
        return "file"
    return "usb"


def get_printer_config():
    """Effective printer config + where each value came from (for the UI)."""
    uri = active_printer_uri()
    return {
        "uri": uri,
        "connection": connection_type_of(uri),
        "model": active_model(),
        "display_name": active_display_name(),
        "sources": {
            "uri": "settings" if (get_setting("printer_uri", "") or "").strip() else "env",
            "model": "settings" if (get_setting("printer_model", "") or "").strip() else "env",
            "display_name": (
                "settings"
                if (get_setting("printer_display_name", "") or "").strip()
                else "env"
            ),
        },
        "env_defaults": {
            "uri": PRINTER,
            "model": MODEL,
            "display_name": PRINTER_DISPLAY_NAME,
        },
        "models": PRINTER_MODELS,
    }


def list_fonts():
    fonts = {}

    for base in FONT_DIR_CANDIDATES:
        if not os.path.isdir(base):
            continue

        for pattern in ("*.ttf", "*.otf", "*.TTF", "*.OTF"):
            for path in glob.glob(
                os.path.join(base, "**", pattern),
                recursive=True
            ):
                name = (
                    os.path.splitext(os.path.basename(path))[0]
                    .replace("-", " ")
                )
                fonts[name] = path

    if not fonts:
        fonts["Default"] = None

    return fonts


AVAILABLE_FONTS = list_fonts()


def get_font(name, size):
    path = AVAILABLE_FONTS.get(name)

    if path:
        try:
            return ImageFont.truetype(path, max(1, int(size)))
        except Exception:
            pass

    return FALLBACK_FONT


def label_canvas(label_size, min_height=None):
    if label_size not in LABEL_SPECS:
        raise ValueError(f"Unknown label size '{label_size}'")

    width, height = LABEL_SPECS[label_size]

    if height is None:
        height = min_height or width

    return Image.new("RGB", (width, height), "white")


def text_size(draw, text, font):
    bbox = draw.textbbox((0, 0), text or " ", font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def render_text(
    content,
    label_size,
    font_name,
    font_size,
    orientation="standard"
):
    font = get_font(font_name, font_size)
    lines = str(content).split("\n") or [""]

    tmp = Image.new("RGB", (10, 10), "white")
    d = ImageDraw.Draw(tmp)

    metrics = []
    for line in lines:
        metrics.append(text_size(d, line, font))

    width_spec, fixed_height = LABEL_SPECS[label_size]

    line_gap = max(3, int(font_size * 0.10))
    total_text_height = (
        sum(h for _, h in metrics)
        + max(0, len(lines) - 1) * line_gap
    )

    height = fixed_height or max(
        total_text_height + 40,
        width_spec // 4
    )

    img = Image.new("RGB", (width_spec, height), "white")
    d = ImageDraw.Draw(img)

    y = max(0, (height - total_text_height) // 2)

    for line, (_, lh) in zip(lines, metrics):
        lw, _ = text_size(d, line, font)
        x = max(0, (width_spec - lw) // 2)

        d.text(
            (x, y),
            line,
            font=font,
            fill="black"
        )

        y += lh + line_gap

    if orientation == "rotated":
        img = img.rotate(90, expand=True)

    return img


def wrap_text(d, text, font, max_width):
    words = str(text).split()

    if not words:
        return [""]

    lines = []
    current = ""

    for word in words:
        candidate = word if not current else current + " " + word
        cw, _ = text_size(d, candidate, font)

        if cw <= max_width:
            current = candidate
            continue

        if current:
            lines.append(current)
            current = ""

        # Break a single very long token.
        token = word
        piece = ""

        for ch in token:
            candidate_piece = piece + ch
            cw2, _ = text_size(d, candidate_piece, font)

            if cw2 <= max_width:
                piece = candidate_piece
            else:
                if piece:
                    lines.append(piece)
                piece = ch

        current = piece

    if current:
        lines.append(current)

    return lines or [""]


def determine_grid_columns(columns_param, count, width_spec, font_size):
    """
    Resolve the "Columns" option (Auto / Fixed number) into an actual
    column count for the Grid / Boxed grid layouts.
    """

    count = max(1, count)

    if columns_param not in (None, "", "auto", "Auto"):
        try:
            requested = int(columns_param)

            if requested > 0:
                return max(1, min(requested, count))

        except (TypeError, ValueError):
            pass

    # Auto: pick a comfortable column count from the available width and
    # the font size, without ever exceeding the number of entries.
    min_cell_width = max(90, int(font_size * 2.2))
    auto_columns = max(1, width_spec // min_cell_width)

    return max(1, min(auto_columns, count, 6))


def clean_text_entries(entries):
    clean_entries = []

    for entry in entries or []:
        if not isinstance(entry, dict):
            continue

        text = str(entry.get("text", "")).strip()
        serial = str(entry.get("serial", "")).strip()

        if text:
            clean_entries.append({
                "text": text,
                "serial": serial
            })

    return clean_entries


def render_text_list(clean_entries, label_size, font_name, font_size, orientation="standard"):
    """
    Plain stacked list: text (and an optional code) for every row.

    No "TEXT" / "UNIQUE CODE" headings, no table lines, no outer box —
    just the content, matching the "no headings or table lines" print
    behavior.
    """

    width_spec, fixed_height = LABEL_SPECS[label_size]

    requested_font_size = max(8, int(font_size))
    body_size = max(10, min(requested_font_size, 48))
    serial_size = max(9, min(body_size, 34))

    body_font = get_font(font_name, body_size)
    serial_font = get_font(font_name, serial_size)

    tmp = Image.new("RGB", (10, 10), "white")
    d = ImageDraw.Draw(tmp)

    left_padding = max(10, int(width_spec * 0.035))
    right_padding = left_padding
    top_padding = max(10, int(width_spec * 0.025))
    bottom_padding = top_padding

    # About 70/30 split. Serial codes remain readable.
    serial_col_width = max(
        105,
        int(width_spec * 0.28)
    )
    text_col_width = max(
        80,
        width_spec - left_padding - right_padding - serial_col_width
    )

    body_gap = max(2, int(body_size * 0.08))
    row_padding_y = max(4, int(body_size * 0.16))

    row_layout = []

    for entry in clean_entries:
        text_lines = wrap_text(
            d,
            entry["text"],
            body_font,
            max(30, text_col_width - 14)
        )

        serial_lines = wrap_text(
            d,
            entry["serial"] or "",
            serial_font,
            max(30, serial_col_width - 14)
        ) if entry["serial"] else []

        text_heights = [
            text_size(d, line, body_font)[1]
            for line in text_lines
        ]

        serial_heights = [
            text_size(d, line, serial_font)[1]
            for line in serial_lines
        ]

        content_h = max(
            sum(text_heights) + max(0, len(text_lines) - 1) * body_gap,
            (sum(serial_heights) + max(0, len(serial_lines) - 1) * body_gap)
            if serial_lines else 0,
            body_size
        )

        row_h = content_h + (row_padding_y * 2)
        row_layout.append((entry, text_lines, serial_lines, row_h))

    calculated_height = (
        top_padding
        + sum(row[3] for row in row_layout)
        + bottom_padding
    )

    if fixed_height:
        height = fixed_height
    else:
        height = max(calculated_height, width_spec // 4)

    img = Image.new("RGB", (width_spec, height), "white")
    d = ImageDraw.Draw(img)

    y = top_padding

    for entry, text_lines, serial_lines, row_h in row_layout:
        text_block_h = (
            sum(text_size(d, line, body_font)[1] for line in text_lines)
            + max(0, len(text_lines) - 1) * body_gap
        )

        serial_block_h = (
            sum(text_size(d, line, serial_font)[1] for line in serial_lines)
            + max(0, len(serial_lines) - 1) * body_gap
        ) if serial_lines else 0

        text_y = y + max(row_padding_y, (row_h - text_block_h) // 2)
        serial_y = y + max(row_padding_y, (row_h - serial_block_h) // 2)

        for line in text_lines:
            d.text(
                (left_padding, text_y),
                line,
                font=body_font,
                fill="black"
            )
            _, lh = text_size(d, line, body_font)
            text_y += lh + body_gap

        for line in serial_lines:
            lw, lh = text_size(d, line, serial_font)
            sx = (
                left_padding
                + text_col_width
                + max(0, (serial_col_width - lw) // 2)
            )

            d.text(
                (sx, serial_y),
                line,
                font=serial_font,
                fill="black"
            )

            serial_y += lh + body_gap

        y += row_h

        if y >= height:
            break

    if orientation == "rotated":
        img = img.rotate(90, expand=True)

    return img


def render_text_grid(
    clean_entries,
    label_size,
    font_name,
    font_size,
    orientation="standard",
    columns="auto",
    boxed=False
):
    """
    Arrange entries into a grid of cells (text + optional code, centered).
    When boxed=True, grid lines are drawn around every cell so the layout
    reads as a "Boxed grid" of individual labels.
    """

    width_spec, fixed_height = LABEL_SPECS[label_size]

    requested_font_size = max(8, int(font_size))
    body_size = max(10, min(requested_font_size, 48))
    serial_size = max(9, min(body_size, 34))

    body_font = get_font(font_name, body_size)
    serial_font = get_font(font_name, serial_size)

    tmp = Image.new("RGB", (10, 10), "white")
    d = ImageDraw.Draw(tmp)

    left_padding = max(10, int(width_spec * 0.03))
    right_padding = left_padding
    top_padding = max(10, int(width_spec * 0.03))
    bottom_padding = top_padding

    count = len(clean_entries) or 1
    cols = determine_grid_columns(columns, count, width_spec, body_size)
    rows = max(1, -(-count // cols))  # ceil division

    grid_left = left_padding
    grid_right = width_spec - right_padding
    grid_width = max(1, grid_right - grid_left)
    cell_w = grid_width / cols

    cell_pad_x = max(6, int(cell_w * 0.08))
    cell_pad_y = max(6, int(body_size * 0.22))
    line_gap = max(2, int(body_size * 0.08))
    block_gap = max(4, int(body_size * 0.16))

    cell_data = []
    max_content_h = 0

    for entry in clean_entries:
        text_lines = wrap_text(
            d,
            entry["text"],
            body_font,
            max(20, cell_w - (2 * cell_pad_x))
        )

        serial_lines = wrap_text(
            d,
            entry["serial"] or "",
            serial_font,
            max(20, cell_w - (2 * cell_pad_x))
        ) if entry["serial"] else []

        text_h = (
            sum(text_size(d, line, body_font)[1] for line in text_lines)
            + max(0, len(text_lines) - 1) * line_gap
        )

        serial_h = (
            sum(text_size(d, line, serial_font)[1] for line in serial_lines)
            + max(0, len(serial_lines) - 1) * line_gap
        ) if serial_lines else 0

        content_h = text_h + (block_gap + serial_h if serial_lines else 0)

        cell_data.append((text_lines, serial_lines))
        max_content_h = max(max_content_h, content_h)

    cell_h = max_content_h + (2 * cell_pad_y)

    if fixed_height:
        height = fixed_height
        available = height - top_padding - bottom_padding

        if available > 0:
            # Shrink cells to fit a fixed label height instead of letting
            # the box run past the edge of the label.
            cell_h = max(body_size + (2 * cell_pad_y), available / rows)
    else:
        height = max(
            top_padding + (cell_h * rows) + bottom_padding,
            width_spec // 4
        )

    img = Image.new("RGB", (width_spec, height), "white")
    d = ImageDraw.Draw(img)

    grid_top = top_padding
    grid_bottom = min(height - 1, grid_top + (cell_h * rows))

    if boxed:
        d.rectangle(
            (grid_left, grid_top, grid_right, grid_bottom),
            outline="black",
            width=2
        )

        for c in range(1, cols):
            x = grid_left + (cell_w * c)
            d.line(
                (x, grid_top, x, grid_bottom),
                fill="black",
                width=1
            )

        for r in range(1, rows):
            line_y = grid_top + (cell_h * r)
            d.line(
                (grid_left, line_y, grid_right, line_y),
                fill="black",
                width=1
            )

    for index, (text_lines, serial_lines) in enumerate(cell_data):
        r = index // cols
        c = index % cols

        cell_x0 = grid_left + (cell_w * c)
        cell_y0 = grid_top + (cell_h * r)
        cell_center_x = cell_x0 + (cell_w / 2)

        text_h = (
            sum(text_size(d, line, body_font)[1] for line in text_lines)
            + max(0, len(text_lines) - 1) * line_gap
        )

        serial_h = (
            sum(text_size(d, line, serial_font)[1] for line in serial_lines)
            + max(0, len(serial_lines) - 1) * line_gap
        ) if serial_lines else 0

        block_h = text_h + (block_gap + serial_h if serial_lines else 0)
        y = cell_y0 + max(cell_pad_y, (cell_h - block_h) / 2)

        for line in text_lines:
            lw, lh = text_size(d, line, body_font)
            x = cell_center_x - (lw / 2)

            d.text((x, y), line, font=body_font, fill="black")
            y += lh + line_gap

        if serial_lines:
            y += (block_gap - line_gap)

            for line in serial_lines:
                lw, lh = text_size(d, line, serial_font)
                x = cell_center_x - (lw / 2)

                d.text((x, y), line, font=serial_font, fill="black")
                y += lh + line_gap

        if index + 1 >= rows * cols:
            break

    if orientation == "rotated":
        img = img.rotate(90, expand=True)

    return img


def render_text_line(clean_entries, label_size, font_name, font_size, orientation="standard"):
    """Render all entries on one continuous horizontal line."""
    width_spec, fixed_height = LABEL_SPECS[label_size]
    font = get_font(font_name, max(8, min(int(font_size), 48)))
    tmp = Image.new("RGB", (10, 10), "white")
    d = ImageDraw.Draw(tmp)
    parts = [f"{e['text']} {e['serial']}".strip() for e in clean_entries]
    text = "   |   ".join(parts) or ""
    tw, th = text_size(d, text, font)
    height = fixed_height or max(th + 40, width_spec // 4)
    img = Image.new("RGB", (max(width_spec, tw + 40), height), "white")
    d = ImageDraw.Draw(img)
    d.text((20, max(0, (height-th)//2)), text, font=font, fill="black")
    if orientation == "rotated":
        img = img.rotate(90, expand=True)
    return img


def apply_print_margins(img, left=0, top=0, right=0, bottom=0):
    """Keep page size while adding white printable margins around rendered content."""
    left, top, right, bottom = [max(0, int(x)) for x in (left, top, right, bottom)]
    w, h = img.size
    target_w = max(1, w-left-right)
    target_h = max(1, h-top-bottom)
    if target_w == w and target_h == h:
        return img
    scale = min(target_w / w, target_h / h)
    nw, nh = max(1, int(w*scale)), max(1, int(h*scale))
    resized = img.resize((nw, nh))
    canvas = Image.new("RGB", (w, h), "white")
    canvas.paste(resized, (left + max(0,(target_w-nw)//2), top + max(0,(target_h-nh)//2)))
    return canvas


def render_multiple_texts(
    entries,
    label_size,
    font_name,
    font_size,
    orientation="standard",
    layout="list",
    columns="auto"
):
    """
    Render multiple text/code entries using one of three layouts:

      - "list":  plain stacked rows, no headings, no table lines.
      - "grid":  entries arranged into columns, centered, no borders.
      - "boxed": same grid, with a box drawn around every cell.

    The canvas height grows automatically for an arbitrary number of
    entries. There is intentionally no 200-row application limit.
    """

    clean_entries = clean_text_entries(entries)
    layout = str(layout or "list").strip().lower()

    if layout not in ("list", "line", "grid", "boxed"):
        layout = "list"

    if not clean_entries:
        return render_text_list(clean_entries, label_size, font_name, font_size, orientation)

    if layout == "line":
        return render_text_line(clean_entries, label_size, font_name, font_size, orientation)

    if layout == "list":
        return render_text_list(
            clean_entries,
            label_size,
            font_name,
            font_size,
            orientation
        )

    return render_text_grid(
        clean_entries,
        label_size,
        font_name,
        font_size,
        orientation,
        columns=columns,
        boxed=(layout == "boxed")
    )


def render_qr(content, label_size, orientation="standard"):
    qr = qrcode.QRCode(
        border=1,
        box_size=10
    )

    qr.add_data(content)
    qr.make(fit=True)

    qr_img = qr.make_image(
        fill_color="black",
        back_color="white"
    ).convert("RGB")

    width_spec, fixed_height = LABEL_SPECS[label_size]

    side = min(
        width_spec,
        fixed_height or width_spec
    )

    qr_img = qr_img.resize((side, side))

    height = fixed_height or side

    canvas = Image.new(
        "RGB",
        (width_spec, height),
        "white"
    )

    canvas.paste(
        qr_img,
        (
            (width_spec - side) // 2,
            (height - side) // 2
        )
    )

    if orientation == "rotated":
        canvas = canvas.rotate(90, expand=True)

    return canvas


def render_barcode(
    content,
    label_size,
    symbology="code128",
    orientation="standard"
):
    bc_cls = barcode.get_barcode_class(symbology)

    buf = io.BytesIO()

    bc = bc_cls(
        content,
        writer=ImageWriter()
    )

    bc.write(
        buf,
        options={
            "write_text": True,
            "quiet_zone": 2
        }
    )

    buf.seek(0)

    bc_img = Image.open(buf).convert("RGB")

    width_spec, fixed_height = LABEL_SPECS[label_size]

    scale = width_spec / bc_img.width

    new_size = (
        width_spec,
        max(1, int(bc_img.height * scale))
    )

    bc_img = bc_img.resize(new_size)

    height = fixed_height or new_size[1]

    canvas = Image.new(
        "RGB",
        (width_spec, height),
        "white"
    )

    canvas.paste(
        bc_img,
        (
            0,
            max((height - new_size[1]) // 2, 0)
        )
    )

    if orientation == "rotated":
        canvas = canvas.rotate(90, expand=True)

    return canvas


def render_image_upload(
    file_storage,
    label_size,
    orientation="standard"
):
    img = Image.open(
        file_storage.stream
    ).convert("RGB")

    width_spec, fixed_height = LABEL_SPECS[label_size]

    scale = width_spec / img.width

    new_size = (
        width_spec,
        max(1, int(img.height * scale))
    )

    img = img.resize(new_size)

    height = fixed_height or new_size[1]

    canvas = Image.new(
        "RGB",
        (width_spec, height),
        "white"
    )

    canvas.paste(
        img,
        (
            0,
            max((height - new_size[1]) // 2, 0)
        )
    )

    if orientation == "rotated":
        canvas = canvas.rotate(90, expand=True)

    return canvas



# Unique codes are exactly 5 digits, e.g. "58464".
SERIAL_RE = re.compile(r"^[0-9]{5}$")


def normalize_serial(serial):
    return str(serial or "").strip()


def validate_serial(serial):
    serial = normalize_serial(serial)

    if not SERIAL_RE.fullmatch(serial):
        raise ValueError(
            f"Invalid unique code '{serial}'. "
            "The unique code must be exactly 5 digits, "
            "e.g. 58464."
        )

    return serial


def serial_exists(serial):
    serial = normalize_serial(serial)

    init_db()

    with sqlite3.connect(SERIAL_DB) as conn:
        row = conn.execute(
            "SELECT serial FROM used_serials WHERE serial=?",
            (serial,)
        ).fetchone()

    return row is not None


def get_serial_record(serial):
    serial = normalize_serial(serial)

    init_db()

    with sqlite3.connect(SERIAL_DB) as conn:
        conn.row_factory = sqlite3.Row

        row = conn.execute(
            """
            SELECT serial, text_content, created_at,
                   printed_at, print_count
            FROM used_serials
            WHERE serial=?
            """,
            (serial,)
        ).fetchone()

    return dict(row) if row else None


def reserve_serial(serial, text_content=None):
    """
    Register a manually entered/reused code.

    Existing code:
      - allowed when it belongs to the same text
      - rejected when it belongs to a different text

    New code:
      - inserted into SQLite
    """
    serial = validate_serial(serial)
    text_content = str(text_content or "").strip()

    init_db()

    with sqlite3.connect(SERIAL_DB) as conn:
        row = conn.execute(
            """
            SELECT serial, text_content
            FROM used_serials
            WHERE serial=?
            """,
            (serial,)
        ).fetchone()

        if row:
            existing_text = str(row[1] or "").strip()

            if (
                text_content
                and existing_text
                and existing_text != text_content
            ):
                raise ValueError(
                    f"Code {serial} is already assigned to "
                    f"'{existing_text}'. "
                    f"Please use that code with the same text or "
                    f"choose another code."
                )

            if text_content and not existing_text:
                conn.execute(
                    """
                    UPDATE used_serials
                    SET text_content=?
                    WHERE serial=?
                    """,
                    (text_content, serial)
                )

            return serial

        conn.execute(
            """
            INSERT INTO used_serials(
                serial,
                text_content,
                created_at,
                printed_at,
                print_count
            )
            VALUES (?, ?, ?, NULL, 0)
            """,
            (
                serial,
                text_content,
                datetime.now(timezone.utc).isoformat()
            )
        )

    return serial


def generate_unique_serial(text_content=None):
    init_db()

    for _ in range(10000):
        # Generate an exactly 5-digit unique number, e.g. 58464.
        digits = f"{random.randint(0, 99999):05d}"
        serial = digits

        try:
            with sqlite3.connect(SERIAL_DB) as conn:
                conn.execute(
                    """
                    INSERT INTO used_serials(
                        serial,
                        text_content,
                        created_at
                    )
                    VALUES (?, ?, ?)
                    """,
                    (
                        serial,
                        str(text_content or "").strip(),
                        datetime.now(timezone.utc).isoformat()
                    )
                )

            return serial

        except sqlite3.IntegrityError:
            continue

    raise RuntimeError(
        "Could not generate a unique code."
    )


def mark_serial_printed(
    serial,
    text_content=None,
    copies=1
):
    serial = normalize_serial(serial)

    if not SERIAL_RE.fullmatch(serial):
        return

    init_db()

    now = datetime.now(timezone.utc).isoformat()
    copies = max(1, int(copies))

    with sqlite3.connect(SERIAL_DB) as conn:
        conn.execute(
            """
            UPDATE used_serials
            SET text_content=COALESCE(?, text_content),
                printed_at=?,
                print_count=print_count + ?
            WHERE serial=?
            """,
            (
                str(text_content).strip()
                if text_content is not None
                else None,
                now,
                copies,
                serial
            )
        )


def expand_template(template, n):
    return str(template).replace(
        "{n}",
        str(n)
    )


def parse_bool(value):
    return str(value).lower() in {
        "1", "true", "yes", "on"
    }


def parse_payload_from_request():
    if (
        request.content_type
        and "multipart/form-data" in request.content_type
    ):
        payload = dict(request.form)

        # JSON fields sent through FormData.
        for key in ("text_entries", "texts", "batch"):
            if key in payload:
                import json

                try:
                    payload[key] = json.loads(payload[key])
                except Exception:
                    pass

        return payload

    return request.get_json(force=True) or {}


def build_images_for_request(payload, files=None):
    kind = payload.get("kind", "text")
    label_size = payload.get(
        "label_size",
        DEFAULT_LABEL_SIZE
    )
    orientation = payload.get(
        "orientation",
        "standard"
    )

    if label_size not in LABEL_SPECS:
        raise ValueError(
            f"Unknown label size '{label_size}'."
        )

    batch = payload.get("batch")

    def with_margins(img):
        return apply_print_margins(
            img,
            payload.get("margin_left", 0), payload.get("margin_top", 0),
            payload.get("margin_right", 0), payload.get("margin_bottom", 0)
        )

    def make_one(content):
        if kind == "text":
            entries = (
                payload.get("text_entries")
                or payload.get("texts")
            )

            if isinstance(entries, list) and entries:
                return with_margins(render_multiple_texts(
                    entries,
                    label_size,
                    payload.get("font", "Default"),
                    int(payload.get("font_size", 60)),
                    orientation,
                    layout=payload.get("layout", "list"),
                    columns=payload.get("columns", "auto")
                ))

            return with_margins(render_text(
                content,
                label_size,
                payload.get("font", "Default"),
                int(payload.get("font_size", 60)),
                orientation
            ))

        if kind == "qr":
            return render_qr(
                content,
                label_size,
                orientation
            )

        if kind == "barcode":
            return render_barcode(
                content,
                label_size,
                payload.get(
                    "symbology",
                    "code128"
                ),
                orientation
            )

        raise ValueError(
            f"Unsupported kind: {kind}"
        )

    if kind == "image":
        if not files or "image" not in files:
            raise ValueError(
                "No image file uploaded."
            )

        return [
            render_image_upload(
                files["image"],
                label_size,
                orientation
            )
        ]

    if batch and parse_bool(batch.get("enabled")):
        start = int(batch["start"])
        end = int(batch["end"])
        step = int(batch.get("step", 1))

        template = batch.get(
            "template",
            "{n}"
        )

        if step <= 0 or end < start:
            raise ValueError(
                "Invalid batch range."
            )

        values = range(
            start,
            end + 1,
            step
        )

        # No artificial 200-label application limit.
        # Brother/printer limitations still apply to the actual print job.
        return [
            make_one(
                expand_template(template, n)
            )
            for n in values
        ]

    content = payload.get(
        "content",
        ""
    )

    copies = max(
        1,
        int(payload.get("copies", 1))
    )

    return [
        make_one(content)
        for _ in range(copies)
    ]


def validate_and_prepare_text_entries(entries):
    if not isinstance(entries, list):
        raise ValueError(
            "text_entries must be a list."
        )

    clean_entries = []
    seen = {}

    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            continue

        text = str(
            entry.get("text", "")
        ).strip()

        serial = normalize_serial(
            entry.get("serial", "")
        )

        if not text:
            continue

        if serial:
            serial = validate_serial(serial)

            # Same code appearing twice in the same print table is
            # almost certainly a data-entry mistake.
            if serial in seen:
                previous_text = seen[serial]

                if previous_text != text:
                    raise ValueError(
                        f"Code {serial} is entered more than once "
                        f"for different texts: '{previous_text}' "
                        f"and '{text}'."
                    )
            else:
                seen[serial] = text

        clean_entries.append({
            "text": text,
            "serial": serial
        })

    if not clean_entries:
        raise ValueError(
            "Enter at least one text row."
        )

    return clean_entries


def prepare_text_serials(entries, generate_missing=True):
    """
    Validate manual codes and optionally generate only missing codes.

    Manual existing code is never replaced.
    """
    clean_entries = validate_and_prepare_text_entries(
        entries
    )

    prepared = []

    for entry in clean_entries:
        text = entry["text"]
        serial = entry["serial"]

        if serial:
            # Existing code may be reused with the same text.
            # New manually entered code is registered.
            reserve_serial(
                serial,
                text
            )
        elif generate_missing:
            serial = generate_unique_serial(text)
        else:
            # Missing codes are valid when Preview/Print are called.
            # The label will contain only the text.
            serial = ""

        prepared.append({
            "text": text,
            "serial": serial
        })

    return prepared


# ---------------------------------------------------------------------------
# Printer discovery / probing
#
# The old code reported every usb:// URI as "present" without ever touching
# USB, so the UI showed a green "Printer ready" dot even while the printer
# was unplugged -- and then Print failed with "unreachable". These helpers
# probe the real hardware so /api/status, diagnostics and the retry loop
# all share one source of truth.
# ---------------------------------------------------------------------------

_USB_URI_RE = re.compile(
    r"^usb://0x([0-9a-fA-F]{4}):0x([0-9a-fA-F]{4})(?:[_/](.*))?$"
)

# Printer-reported errors that are worth one immediate retry because they
# usually indicate a transient USB transfer problem rather than a physical
# printer state (open cover, missing labels, ...).
_RETRYABLE_PRINTER_ERRORS = {
    "Transmission / Communication error",
}


def parse_usb_uri(uri):
    """Parse 'usb://0xVVVV:0xPPPP[_serial]' -> (vid, pid, serial|None)."""
    if not isinstance(uri, str):
        return None
    match = _USB_URI_RE.match(uri.strip())
    if not match:
        # Also accept the short '0xVVVV:0xPPPP' form brother_ql understands.
        short = re.match(
            r"^0x([0-9a-fA-F]{4}):0x([0-9a-fA-F]{4})$", uri.strip()
        )
        if not short:
            return None
        return int(short.group(1), 16), int(short.group(2), 16), None
    return (
        int(match.group(1), 16),
        int(match.group(2), 16),
        match.group(3) or None,
    )


_TCP_URI_RE = re.compile(r"^tcp://([^:/\s]+)(?::(\d+))?/?$")


def parse_tcp_uri(uri):
    """Parse 'tcp://HOST[:PORT]' -> (host, port). Default port 9100."""
    if not isinstance(uri, str):
        return None
    match = _TCP_URI_RE.match(uri.strip())
    if not match:
        return None
    host = match.group(1)
    try:
        port = int(match.group(2)) if match.group(2) else NETWORK_PRINTER_PORT
    except (TypeError, ValueError):
        return None
    if not 1 <= port <= 65535:
        return None
    return host, port


def probe_tcp_uri(uri, timeout=None):
    """Check whether a tcp:// printer answers on its raw-socket port.

    Opens a short TCP connection (default 3 s timeout) without sending
    any print data, so this is safe to call from status polls.
    """
    parsed = parse_tcp_uri(uri)
    if parsed is None:
        return {
            "present": False,
            "uri": uri,
            "backend": "network",
            "error": (
                f"Unrecognised network printer URI '{uri}'. Expected "
                "'tcp://HOST[:PORT]' (e.g. tcp://192.168.1.50:9100)."
            ),
        }
    host, port = parsed
    timeout = NETWORK_PROBE_TIMEOUT if timeout is None else timeout
    start = time.time()
    sock = None
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except socket.timeout:
        return {
            "present": False,
            "uri": uri,
            "backend": "network",
            "host": host,
            "port": port,
            "error": (
                f"Connection to {host}:{port} timed out after "
                f"{timeout:.0f}s. The printer may be offline, asleep, "
                "or unreachable from this network."
            ),
        }
    except socket.gaierror:
        return {
            "present": False,
            "uri": uri,
            "backend": "network",
            "host": host,
            "port": port,
            "error": (
                f"Could not resolve host '{host}'. Check the printer's "
                "IP address / hostname."
            ),
        }
    except OSError as e:
        return {
            "present": False,
            "uri": uri,
            "backend": "network",
            "host": host,
            "port": port,
            "error": f"Could not reach {host}:{port}: {e}",
        }
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
    return {
        "present": True,
        "uri": uri,
        "backend": "network",
        "host": host,
        "port": port,
        "latency_ms": round((time.time() - start) * 1000),
    }


def usb_backend_available():
    """Check that libusb is usable from inside this process/container."""
    try:
        # A no-match find still initialises the backend; if libusb is
        # missing pyusb raises NoBackendError here.
        list(usb.core.find(find_all=True, idVendor=0xFFFF, idProduct=0xFFFF) or [])
        return True, None
    except usb.core.NoBackendError as e:
        return False, (
            "No USB backend available (libusb is missing or libusb-1.0 "
            f"could not be loaded): {e}"
        )
    except Exception as e:  # pragma: no cover - defensive
        return False, f"USB backend probe failed: {e}"


def list_brother_usb_devices():
    """Return every Brother printer visible on the USB bus.

    Each entry is a dict with identifier/bus/address/vid/pid/serial and an
    optional 'error' field when descriptor reads fail (common while the
    device is resuming from USB autosuspend).
    """
    devices = []
    try:
        found = usb.core.find(find_all=True, idVendor=0x04F9) or []
    except usb.core.NoBackendError as e:
        return [], f"No USB backend: {e}"
    except Exception as e:
        return [], f"USB scan failed: {e}"

    for dev in found:
        entry = {
            "identifier": (
                f"usb://0x{dev.idVendor:04x}:0x{dev.idProduct:04x}"
            ),
            "bus": getattr(dev, "bus", None),
            "address": getattr(dev, "address", None),
            "vid": f"0x{dev.idVendor:04x}",
            "pid": f"0x{dev.idProduct:04x}",
        }
        try:
            serial = None
            if getattr(dev, "iSerialNumber", 0):
                try:
                    serial = usb.util.get_string(
                        dev, 256, dev.iSerialNumber
                    )
                except Exception:
                    serial = None
            if serial:
                entry["serial"] = serial
                entry["identifier"] = (
                    f"usb://0x{dev.idVendor:04x}:0x{dev.idProduct:04x}"
                    f"_{serial}"
                )
            try:
                entry["kernel_driver_active"] = bool(
                    dev.is_kernel_driver_active(0)
                )
            except NotImplementedError:
                entry["kernel_driver_active"] = None
            except Exception:
                entry["kernel_driver_active"] = None
        except Exception as e:
            entry["error"] = (
                "Descriptor read failed (device may be resuming from "
                f"sleep/autosuspend): {e}"
            )
        devices.append(entry)
    return devices, None


def list_linux_kernel_devices():
    """Return /dev/usb/lp* nodes visible inside this container."""
    paths = sorted(glob.glob("/dev/usb/lp*"))
    devices = []
    for path in paths:
        devices.append(
            {
                "identifier": f"file://{path}",
                "path": path,
                "readable": os.access(path, os.R_OK),
                "writable": os.access(path, os.W_OK),
            }
        )
    return devices


def probe_usb_uri(uri):
    """Check whether a usb:// URI currently resolves to a live device."""
    parsed = parse_usb_uri(uri)
    if parsed is None:
        return {
            "present": False,
            "uri": uri,
            "error": (
                f"Unrecognised USB printer URI '{uri}'. Expected "
                "'usb://0xVVVV:0xPPPP' (e.g. usb://0x04f9:0x209b)."
            ),
        }
    vid, pid, _serial = parsed
    ok, backend_error = usb_backend_available()
    if not ok:
        return {"present": False, "uri": uri, "error": backend_error}
    try:
        dev = usb.core.find(idVendor=vid, idProduct=pid)
    except usb.core.NoBackendError as e:
        return {"present": False, "uri": uri, "error": f"No USB backend: {e}"}
    except Exception as e:
        return {
            "present": False,
            "uri": uri,
            "error": f"USB lookup failed: {e}",
        }
    if dev is None:
        return {
            "present": False,
            "uri": uri,
            "vid": f"0x{vid:04x}",
            "pid": f"0x{pid:04x}",
            "error": "Device not found on the USB bus.",
        }
    info = {
        "present": True,
        "uri": uri,
        "vid": f"0x{vid:04x}",
        "pid": f"0x{pid:04x}",
        "bus": getattr(dev, "bus", None),
        "address": getattr(dev, "address", None),
    }
    try:
        if getattr(dev, "iSerialNumber", 0):
            info["serial"] = usb.util.get_string(
                dev, 256, dev.iSerialNumber
            )
    except Exception:
        pass
    return info


def probe_printer_uri(uri):
    """Probe any supported printer URI (usb://, file://, /dev/..., tcp://)."""
    uri = str(uri or "").strip()
    if uri.startswith("usb://") or uri.startswith("0x"):
        result = probe_usb_uri(uri)
        result["backend"] = "pyusb"
        return result
    if uri.startswith("file://") or uri.startswith("/dev/"):
        path = uri[7:] if uri.startswith("file://") else uri
        result = {
            "uri": uri,
            "backend": "linux_kernel",
            "path": path,
            "present": os.path.exists(path),
            "readable": os.access(path, os.R_OK),
            "writable": os.access(path, os.W_OK),
        }
        if not result["present"]:
            result["error"] = f"Device node {path} does not exist."
        elif not (result["readable"] and result["writable"]):
            result["error"] = (
                f"Device node {path} is not readable/writable by this "
                "process (udev permissions)."
            )
        return result
    if uri.startswith("tcp://"):
        # A short TCP connect is cheap and safe (no data sent), so Wi-Fi
        # printers get a real present/absent answer just like USB ones.
        return probe_tcp_uri(uri)
    return {
        "uri": uri,
        "backend": None,
        "present": False,
        "error": f"Unsupported printer URI '{uri}'.",
    }


def probe_printer():
    """Full printer health snapshot for /api/status and diagnostics."""
    printer_uri = active_printer_uri()
    backend_ok, backend_error = usb_backend_available()
    usb_devices, usb_scan_error = list_brother_usb_devices()
    kernel_devices = list_linux_kernel_devices()
    primary = probe_printer_uri(printer_uri)

    present = bool(primary.get("present"))
    detail = primary.get("error") or primary.get("note")

    if not present and PRINTER_AUTO_FALLBACK:
        # A fallback that IS present is worth surfacing: printing may
        # still succeed via the alternate backend.
        for candidate in resolve_printer_candidates()[1:]:
            alt = probe_printer_uri(candidate)
            if alt.get("present"):
                detail = (
                    (detail + " " if detail else "")
                    + f"Primary {printer_uri} is not visible, but fallback "
                    f"{candidate} is present and will be tried."
                )
                break

    return {
        "app": APP_NAME,
        "version": APP_VERSION,
        "model": active_model(),
        "printer_display_name": active_display_name(),
        "printer": printer_uri,
        "connection": connection_type_of(printer_uri),
        "device_present": present,
        "device_path": printer_device_path(),
        "primary": primary,
        "detail": detail,
        "usb_backend_available": backend_ok,
        "usb_backend_error": backend_error,
        "usb_scan_error": usb_scan_error,
        "usb_devices": usb_devices,
        "kernel_devices": kernel_devices,
        "fallbacks": resolve_printer_candidates(),
        "last_error": _PRINTER_STATE["last_error"],
        "last_error_at": _PRINTER_STATE["last_error_at"],
        "last_success_at": _PRINTER_STATE["last_success_at"],
        "consecutive_failures": _PRINTER_STATE["consecutive_failures"],
        "last_printer_used": _PRINTER_STATE["last_printer_used"],
    }


def printer_device_present():
    """True when the configured printer looks reachable right now.

    Unlike the old implementation (which returned True for every usb://
    URI without checking), this actually probes the USB bus / device
    node, so the UI status dot reflects reality.
    """
    try:
        return bool(probe_printer_uri(active_printer_uri()).get("present"))
    except Exception:
        return False


def printer_device_path():
    uri = active_printer_uri()
    if uri.startswith("file://"):
        return uri.replace(
            "file://",
            "",
            1
        )

    return uri


def resolve_printer_candidates():
    """Ordered list of printer URIs to try (primary first).

    Besides the explicit PRINTER_FALLBACKS env var, auto-discovery adds:
      * any Brother pyusb device found on the bus (handles the case where
        the configured PID is slightly off, e.g. QL-800 vs QL-810W), and
      * any /dev/usb/lp* node (handles hosts where usblp claimed the
        printer and pyusb cannot detach it).

    Auto-discovery is skipped when the primary is a network (Wi-Fi)
    printer: a USB device on this host would be a *different physical*
    printer, and silently printing there would be surprising. Explicit
    PRINTER_FALLBACKS are still honoured for network primaries.
    """
    candidates = []
    seen = set()

    def add(uri):
        uri = str(uri or "").strip()
        if uri and uri not in seen:
            seen.add(uri)
            candidates.append(uri)

    primary = active_printer_uri()
    add(primary)
    for fallback in PRINTER_FALLBACKS:
        add(fallback)

    if PRINTER_AUTO_FALLBACK and connection_type_of(primary) != "network":
        try:
            usb_devices, _ = list_brother_usb_devices()
        except Exception:
            usb_devices = []
        configured = parse_usb_uri(primary)
        for entry in usb_devices:
            ident = entry.get("identifier")
            if not ident:
                continue
            if configured is not None:
                found = parse_usb_uri(ident)
                # Prefer an exact VID:PID match; still add other Brother
                # printers afterwards as a last resort.
                if found is not None and found[:2] == configured[:2]:
                    add(ident if "_" not in ident else
                        f"usb://0x{found[0]:04x}:0x{found[1]:04x}")
                else:
                    add(ident)
            else:
                add(ident)
        for entry in list_linux_kernel_devices():
            add(entry.get("identifier"))

    return candidates or [primary]


def _is_retryable_open_error(exc):
    """Classify open/write failures into retryable vs fatal.

    Returns (retryable: bool, reason: str).
    """
    if isinstance(exc, usb.core.NoBackendError):
        return False, (
            "libusb backend is missing inside the container "
            f"({exc}). Printing cannot work until libusb-1.0 is "
            "installed and /dev/bus/usb is mapped."
        )
    if isinstance(exc, ValueError):
        message = str(exc)
        # brother_ql's pyusb backend raises plain ValueError
        # ("Device not found") when its usb.core.find() scan comes up
        # empty mid-enumeration -- transient, deserves a retry.
        if message == "Device not found":
            return True, message
        if "Cannot guess backend" in message:
            return False, message
        # NoBackendError subclasses ValueError on some pyusb versions.
        if "No backend" in message:
            return False, message
        return False, message
    if isinstance(exc, AssertionError):
        # brother_ql asserts on interface/endpoint descriptors; a waking
        # device can briefly return incomplete descriptors.
        return True, (
            "Printer USB descriptors were temporarily unreadable "
            f"(device may be waking): {exc}"
        )
    if isinstance(exc, TypeError) and "not iterable" in str(exc):
        # Some pyusb/libusb combinations return None instead of [] from
        # find(find_all=True) while the bus is re-enumerating; brother_ql
        # then crashes iterating the scan result. Semantically identical
        # to "Device not found" -- transient, deserves a retry.
        return True, "Device not found"
    if isinstance(exc, usb.core.USBError):
        text = str(exc).lower()
        # Permission / busy errors are usually persistent config issues,
        # but a re-enumerating device can briefly report them, so allow
        # the normal retry loop to ride through short blips while still
        # surfacing a precise final message.
        return True, str(exc)
    if isinstance(exc, OSError):
        return True, str(exc)
    return False, str(exc)


def _send_once(instructions, printer_uri):
    """Send one job via a freshly opened backend handle.

    brother_ql's helpers.send() never explicitly disposes the backend, so
    a failed attempt can leak a claimed USB interface / detached kernel
    driver. This version owns the lifecycle with try/finally and reads
    back the printer status itself.
    """
    backend_id = guess_backend(printer_uri)
    factory = backend_factory(backend_id)
    backend_cls = factory["backend_class"]
    printer = backend_cls(printer_uri)
    try:
        printer.write(instructions)
        outcome = "sent"
        printer_state = None
        did_print = False
        ready_for_next = False

        if backend_id == "network":
            return {
                "outcome": outcome,
                "printer_state": None,
                "did_print": False,
                "ready_for_next_job": False,
                "backend": backend_id,
            }

        start = time.time()
        while time.time() - start < PRINT_STATUS_TIMEOUT:
            try:
                data = printer.read()
            except Exception as e:
                logger.debug("status read failed (will retry): %s", e)
                time.sleep(0.05)
                continue
            if not data:
                time.sleep(0.005)
                continue
            try:
                result = interpret_response(data)
            except Exception as e:
                logger.debug("unparseable status reply %r: %s", data, e)
                continue
            printer_state = result
            logger.debug("printer status: %s", result)
            if result.get("errors"):
                outcome = "error"
                break
            if result.get("status_type") == "Printing completed":
                did_print = True
                outcome = "printed"
            if (
                result.get("status_type") == "Phase change"
                and result.get("phase_type") == "Waiting to receive"
            ):
                ready_for_next = True
            if did_print and ready_for_next:
                break

        return {
            "outcome": outcome,
            "printer_state": printer_state,
            "did_print": did_print,
            "ready_for_next_job": ready_for_next,
            "backend": backend_id,
        }
    finally:
        try:
            printer.dispose()
        except Exception:
            pass


def _wake_probe(printer_uri):
    """Best-effort wake of a sleeping/autosuspended printer.

    Touching the device (a cheap descriptor read via find) pulls a
    Linux-autosuspended device back to full power; the settle wait then
    gives the QL-800 firmware time to answer the real open. Never raises.
    """
    try:
        parsed = parse_usb_uri(printer_uri)
        if parsed is not None:
            vid, pid, _serial = parsed
            try:
                dev = usb.core.find(idVendor=vid, idProduct=pid)
            except Exception:
                dev = None
            if dev is not None:
                try:
                    # Cheap control transfer: product string read wakes
                    # most autosuspended devices.
                    if getattr(dev, "iProduct", 0):
                        usb.util.get_string(dev, 256, dev.iProduct)
                except Exception:
                    pass
                if PRINT_WAKE_WAIT:
                    time.sleep(PRINT_WAKE_WAIT)
                return True
    except Exception:
        pass
    return False


def _troubleshooting_hint(last_error_text, tried_uris):
    lines = []
    text = (last_error_text or "").lower()
    via_network = any(
        str(u or "").startswith("tcp://") for u in (tried_uris or [])
    )

    if "no backend" in text or "libusb" in text:
        lines.append(
            "- libusb is not available inside the container. Rebuild with "
            "libusb-1.0-0 installed (see Dockerfile) and map the USB bus "
            "with --device=/dev/bus/usb:/dev/bus/usb (or privileged: true)."
        )
    if "device not found" in text or "not found" in text:
        lines.append(
            "- Check the USB cable and that the QL-800 is powered on (the "
            "green LED should be lit). The printer's own auto-off is "
            "separate from Linux USB autosuspend: even with auto-off "
            "disabled the OS can still suspend the port."
        )
        lines.append(
            "- On the Docker host run 'lsusb | grep 04f9' -- if nothing "
            "shows, the container cannot see the printer either. Also "
            "confirm the udev rule for idVendor=04f9 (e.g. "
            'SUBSYSTEM==\"usb\", ATTR{idVendor}==\"04f9\", MODE=\"0666\") '
            "and that the container maps /dev/bus/usb."
        )
        lines.append(
            "- If Unraid/host slept or the cable was re-plugged, the bus "
            "address changes; this app re-scans on every attempt, so just "
            "retry. 'Unplug 10s -> replug -> wait 5s -> retry print' "
            "recovers most cases."
        )
    if "access denied" in text or "permission" in text:
        lines.append(
            "- Permission denied: the container user cannot open the USB "
            "device. Keep privileged: true (or add the device cgroup rule) "
            "and install the udev rule above, then replug the printer."
        )
    if "busy" in text or "resource busy" in text:
        lines.append(
            "- Device busy: another process (often the host's usblp "
            "driver at /dev/usb/lp0, or a second print job) holds the "
            "printer. Wait a few seconds and retry; concurrent prints are "
            "serialised by the app, so this usually clears by itself."
        )
    if "timeout" in text or "timed out" in text:
        lines.append(
            "- USB timeout: try a different USB port/cable, avoid hubs, "
            "and disable OS USB autosuspend for this device "
            "(e.g. echo on > /sys/bus/usb/devices/<dev>/power/control)."
        )
    if "cover" in text or "no media" in text or "end of media" in text:
        lines.append(
            "- The printer itself reports a media/cover problem: check the "
            "label roll is loaded, the cover is closed, and the correct "
            "label size is selected."
        )
    if (
        via_network
        or "connection refused" in text
        or "name or service not known" in text
        or "nodename nor servname" in text
        or "no route to host" in text
        or "network is unreachable" in text
    ):
        lines.append(
            "- Network (Wi-Fi) printer unreachable: confirm the printer is "
            "connected to Wi-Fi (Wi-Fi LED lit), the IP/hostname is "
            "correct, and port 9100 is reachable from the Docker host "
            "(try: nc -zv PRINTER_IP 9100). Give the printer a static IP "
            "or DHCP reservation so the address never changes."
        )
        lines.append(
            "- Note: the QL-800 is USB-only. Wi-Fi printing needs a "
            "QL-810W / QL-820NWB / QL-1110NWB with the matching Model "
            "selected in the Printer panel."
        )
    if not lines:
        lines.append(
            "- Check the cable/power, run 'lsusb | grep 04f9' on the host, "
            "confirm the udev rule for idVendor=04f9 and the "
            "/dev/bus/usb device mapping, then retry."
        )
    if len(tried_uris) > 1:
        lines.append(
            f"- Tried URIs in order: {', '.join(tried_uris)}."
        )
    lines.append(
        "- Open /api/printer/diagnostics in the browser for a live view "
        "of visible USB/kernel devices."
    )
    return "\n".join(lines)


def try_usb_reset(printer_uri=None):
    """Attempt a USB port reset to recover a wedged printer.

    Returns (ok: bool, message: str). Never raises.
    """
    uri = printer_uri or active_printer_uri()
    if connection_type_of(uri) == "network":
        return False, (
            "USB reset only applies to USB printers. For a Wi-Fi "
            "printer, power-cycle it or use Reconnect instead."
        )
    parsed = parse_usb_uri(uri)
    if parsed is None:
        # Try every visible Brother device as a fallback.
        devices, scan_error = list_brother_usb_devices()
        if scan_error:
            return False, scan_error
        if not devices:
            return False, "No Brother USB devices visible to reset."
        # Reset the first visible one via a fresh find.
        try:
            found = usb.core.find(find_all=True, idVendor=0x04F9) or []
        except Exception as e:
            return False, f"USB reset scan failed: {e}"
        target = None
        for dev in found:
            target = dev
            break
        if target is None:
            return False, "No Brother USB devices visible to reset."
        try:
            target.reset()
            time.sleep(2.0)
            return True, "USB device reset issued; retry printing."
        except Exception as e:
            return False, f"USB reset failed: {e}"
    vid, pid, _serial = parsed
    try:
        dev = usb.core.find(idVendor=vid, idProduct=pid)
    except Exception as e:
        return False, f"USB lookup failed: {e}"
    if dev is None:
        return False, (
            f"Device 0x{vid:04x}:0x{pid:04x} not on the bus; "
            "check cable/power."
        )
    try:
        dev.reset()
    except Exception as e:
        text = str(e).lower()
        if "permission" in text or "access" in text:
            return False, (
                f"USB reset needs more privilege: {e}. The container "
                "normally requires privileged: true for reset."
            )
        return False, f"USB reset failed: {e}"
    time.sleep(2.0)
    return True, "USB device reset issued; retry printing."


def print_images(images, label_size):
    if not images:
        raise ValueError(
            "There are no labels to print."
        )

    # Never let two threads interleave USB traffic.
    acquired = _PRINT_LOCK.acquire(timeout=30)
    if not acquired:
        raise RuntimeError(
            "Another print job is already in progress. "
            "Wait a few seconds and try again."
        )
    try:
        return _print_images_locked(images, label_size)
    finally:
        _PRINT_LOCK.release()


def _print_images_locked(images, label_size):
    qlr = BrotherQLRaster(active_model())
    qlr.exception_on_warning = True

    convert(
        qlr=qlr,
        images=images,
        label=label_size,
        rotate="0",
        threshold=70.0,
        dither=False,
        compress=False,
        red=False,
        dpi_600=False,
        hq=True,
        cut=True
    )

    candidates = resolve_printer_candidates()
    tried = []
    last_error = None
    last_error_text = ""
    attempts_log = []

    for uri in candidates:
        # Primary URI gets the full retry budget; fallbacks get a
        # shorter budget (they are only attempted after the primary
        # already failed).
        budget = (
            PRINT_MAX_RETRIES if uri == candidates[0]
            else min(PRINT_MAX_RETRIES, 2)
        )
        for attempt in range(1, budget + 1):
            if uri not in tried:
                tried.append(uri)
            # From the 2nd attempt on, actively try to wake a sleeping
            # / autosuspended device before opening it.
            if attempt > 1:
                _wake_probe(uri)
            try:
                logger.info(
                    "print attempt %d/%d via %s (%d bytes)",
                    attempt, budget, uri, len(qlr.data),
                )
                if connection_type_of(uri) == "network":
                    # brother_ql's network backend calls blocking
                    # connect() with no timeout: bound it so a Wi-Fi
                    # printer that drops mid-job can't hang the
                    # request for minutes. (Probes pass explicit
                    # timeouts, so they are unaffected; prints are
                    # serialised by _PRINT_LOCK.)
                    previous_timeout = socket.getdefaulttimeout()
                    socket.setdefaulttimeout(15)
                    try:
                        result = _send_once(qlr.data, uri)
                    finally:
                        socket.setdefaulttimeout(previous_timeout)
                else:
                    result = _send_once(qlr.data, uri)
            except Exception as e:
                retryable, reason = _is_retryable_open_error(e)
                last_error = e
                last_error_text = f"{type(e).__name__}: {e}"
                attempts_log.append(f"{uri} attempt {attempt}: {e}")
                logger.warning(
                    "[print_images] %s error on attempt %d/%d: %s",
                    uri, attempt, budget, e,
                )
                if not retryable:
                    # Fatal configuration errors (missing libusb, bad
                    # URI, ...) still get the troubleshooting hints so
                    # the user sees an actionable message instead of a
                    # bare "No backend available".
                    now = datetime.now(timezone.utc).isoformat()
                    _PRINTER_STATE["last_error"] = last_error_text
                    _PRINTER_STATE["last_error_at"] = now
                    _PRINTER_STATE["consecutive_failures"] = (
                        _PRINTER_STATE["consecutive_failures"] + 1
                    )
                    hint = _troubleshooting_hint(
                        f"{e} {reason}", tried or [uri]
                    )
                    raise RuntimeError(
                        f"Printer error via {uri}: {e}\n\n{hint}"
                    ) from e
                if attempt < budget:
                    # Progressive backoff: waking firmware can need a
                    # few seconds, longer than one fixed delay.
                    delay = min(8.0, PRINT_RETRY_DELAY * (1 + 0.5 * (attempt - 1)))
                    time.sleep(delay)
                    continue
                break  # budget exhausted for this URI -> next candidate

            # The bytes were accepted; now interpret the printer status.
            state = result.get("printer_state") or {}
            errors = list(state.get("errors") or [])
            if errors:
                retryable_errors = [
                    err for err in errors
                    if err in _RETRYABLE_PRINTER_ERRORS
                ]
                if retryable_errors and len(retryable_errors) == len(errors) and attempt < budget:
                    last_error = RuntimeError("; ".join(errors))
                    last_error_text = "; ".join(errors)
                    attempts_log.append(
                        f"{uri} attempt {attempt}: transient printer "
                        f"error: {'; '.join(errors)}"
                    )
                    logger.warning(
                        "[print_images] transient printer error, "
                        "retrying: %s", "; ".join(errors)
                    )
                    time.sleep(PRINT_RETRY_DELAY)
                    continue
                # Physical printer state (cover open, no media, ...):
                # retrying cannot fix it, fail fast with a clear message.
                now = datetime.now(timezone.utc).isoformat()
                _PRINTER_STATE["last_error"] = "; ".join(errors)
                _PRINTER_STATE["last_error_at"] = now
                _PRINTER_STATE["consecutive_failures"] = (
                    _PRINTER_STATE["consecutive_failures"] + 1
                )
                raise RuntimeError(
                    "Printer reported an error: "
                    + "; ".join(errors)
                    + f"\n(Media: {state.get('media_type', '?')}, "
                    f"width={state.get('media_width', '?')}, "
                    f"status={state.get('status_type', '?')}).\n"
                    "Check the label roll, close the cover, then retry."
                )

            # Success (or uncertain-but-sent). did_print/ready flags are
            # informational: some firmware revisions never emit the full
            # sequence even though the label printed. The network
            # backend never reads status back at all, so 'sent' is a
            # full success for Wi-Fi printers, not a warning.
            now = datetime.now(timezone.utc).isoformat()
            _PRINTER_STATE["last_error"] = None
            _PRINTER_STATE["last_error_at"] = None
            _PRINTER_STATE["last_success_at"] = now
            _PRINTER_STATE["consecutive_failures"] = 0
            _PRINTER_STATE["last_printer_used"] = uri
            if result.get("backend") == "network":
                logger.info("[print_images] sent via %s (network)", uri)
                return {"uri": uri, "backend": "network"}
            if not result.get("did_print") or not result.get("ready_for_next_job"):
                logger.warning(
                    "[print_images] sent via %s but completion status "
                    "was not fully confirmed (did_print=%s ready=%s); "
                    "the label may still have printed.",
                    uri, result.get("did_print"),
                    result.get("ready_for_next_job"),
                )
                return {
                    "uri": uri,
                    "backend": result.get("backend"),
                    "warning": (
                        "Label data was sent but the printer did not "
                        "confirm completion. Check the printer output; "
                        "if the label printed, no action is needed."
                    ),
                }
            logger.info("[print_images] printed via %s", uri)
            return {"uri": uri, "backend": result.get("backend")}

    now = datetime.now(timezone.utc).isoformat()
    _PRINTER_STATE["last_error"] = last_error_text or "unknown error"
    _PRINTER_STATE["last_error_at"] = now
    _PRINTER_STATE["consecutive_failures"] = (
        _PRINTER_STATE["consecutive_failures"] + 1
    )
    hint = _troubleshooting_hint(last_error_text, tried)
    raise RuntimeError(
        "Printer unreachable after "
        f"{PRINT_MAX_RETRIES} attempt(s): {last_error_text}\n\n"
        f"{hint}"
    ) from last_error


def image_to_data_url(img):
    buf = io.BytesIO()

    img.save(
        buf,
        format="PNG"
    )

    return (
        "data:image/png;base64,"
        + base64.b64encode(
            buf.getvalue()
        ).decode("ascii")
    )


@app.route("/")
def index():
    context = dict(
        app_name=APP_NAME,
        app_version=APP_VERSION,
        model=active_model(),
        printer_display_name=active_display_name(),
        printer_models=PRINTER_MODELS,
        label_sizes=sorted(
            LABEL_SPECS.keys()
        ),
        fonts=sorted(
            AVAILABLE_FONTS.keys()
        ),
        default_label_size=DEFAULT_LABEL_SIZE,
    )
    try:
        return render_template("index.html", **context)
    except Exception:
        # The repo keeps index.html next to app.py (flat upload) while
        # Flask looks in templates/ -- render the sibling file directly
        # so "/" works in both layouts.
        sibling = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "index.html"
        )
        if os.path.exists(sibling):
            with open(sibling, "r", encoding="utf-8") as fh:
                return render_template_string(fh.read(), **context)
        raise


@app.route("/api/fonts")
def api_fonts():
    return jsonify(
        sorted(AVAILABLE_FONTS.keys())
    )


@app.route("/api/serial/new", methods=["POST"])
def api_new_serial():
    try:
        payload = request.get_json(
            force=True,
            silent=True
        ) or {}

        text = str(
            payload.get("text", "")
        ).strip()

        return jsonify({
            "ok": True,
            "serial": generate_unique_serial(text)
        })

    except Exception as e:
        traceback.print_exc()

        return jsonify({
            "ok": False,
            "error": str(e)
        }), 400


@app.route("/api/serial/check", methods=["POST"])
def api_check_serial():
    try:
        payload = request.get_json(
            force=True
        ) or {}

        serial = validate_serial(
            payload.get("serial", "")
        )

        text = str(
            payload.get("text", "")
        ).strip()

        record = get_serial_record(
            serial
        )

        if not record:
            return jsonify({
                "ok": True,
                "exists": False,
                "available": True,
                "serial": serial
            })

        existing_text = str(
            record.get("text_content") or ""
        ).strip()

        compatible = (
            not existing_text
            or not text
            or existing_text == text
        )

        return jsonify({
            "ok": True,
            "exists": True,
            "available": compatible,
            "serial": serial,
            "text_content": existing_text,
            "record": record
        })

    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e)
        }), 400


@app.route("/api/serial/register", methods=["POST"])
def api_register_serial():
    try:
        payload = request.get_json(
            force=True
        ) or {}

        serial = validate_serial(
            payload.get("serial", "")
        )

        text = str(
            payload.get("text", "")
        ).strip()

        if not text:
            raise ValueError(
                "Text is required when registering a code."
            )

        reserve_serial(
            serial,
            text
        )

        return jsonify({
            "ok": True,
            "serial": serial
        })

    except Exception as e:
        traceback.print_exc()

        return jsonify({
            "ok": False,
            "error": str(e)
        }), 400


@app.route("/api/serial/generate-all", methods=["POST"])
def api_generate_all_serials():
    """
    Generate only missing serials.

    Existing/manual serials are validated and kept unchanged.
    """
    try:
        payload = request.get_json(
            force=True
        ) or {}

        entries = payload.get(
            "entries"
        )

        if entries is None:
            # Backwards-compatible support for the old frontend.
            texts = payload.get(
                "texts",
                []
            )

            if not isinstance(texts, list):
                raise ValueError(
                    "texts must be a list."
                )

            entries = [
                {
                    "text": str(text).strip(),
                    "serial": ""
                }
                for text in texts
            ]

        prepared = prepare_text_serials(
            entries,
            generate_missing=True
        )

        return jsonify({
            "ok": True,
            "entries": prepared,
            "serials": [
                x["serial"]
                for x in prepared
            ]
        })

    except Exception as e:
        traceback.print_exc()

        return jsonify({
            "ok": False,
            "error": str(e)
        }), 400


@app.route("/api/summary/delete", methods=["POST"])
def api_delete_summary_record():
    try:
        payload = request.get_json(force=True) or {}
        serial = normalize_serial(payload.get("serial", ""))
        if not serial:
            raise ValueError("Serial is required.")
        init_db()
        with sqlite3.connect(SERIAL_DB) as conn:
            cur = conn.execute("DELETE FROM used_serials WHERE serial=?", (serial,))
            if cur.rowcount == 0:
                raise ValueError("Serial record not found.")
        return jsonify({"ok": True, "serial": serial})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/summary")
def api_summary():
    try:
        init_db()

        with sqlite3.connect(
            SERIAL_DB
        ) as conn:
            conn.row_factory = sqlite3.Row

            total_serials = conn.execute(
                "SELECT COUNT(*) FROM used_serials"
            ).fetchone()[0]

            printed_serials = conn.execute(
                """
                SELECT COUNT(*)
                FROM used_serials
                WHERE printed_at IS NOT NULL
                """
            ).fetchone()[0]

            total_prints = conn.execute(
                """
                SELECT COALESCE(
                    SUM(print_count),
                    0
                )
                FROM used_serials
                """
            ).fetchone()[0]

            recent = conn.execute(
                """
                SELECT
                    serial,
                    text_content,
                    created_at,
                    printed_at,
                    print_count
                FROM used_serials
                ORDER BY
                    COALESCE(
                        printed_at,
                        created_at
                    ) DESC
                LIMIT 100
                """
            ).fetchall()

        return jsonify({
            "ok": True,
            "total_serials": total_serials,
            "printed_serials": printed_serials,
            "total_prints": total_prints,
            "recent": [
                dict(r)
                for r in recent
            ]
        })

    except Exception as e:
        traceback.print_exc()

        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500


@app.route("/api/preview", methods=["POST"])
def api_preview():
    """
    Preview NEVER accesses the printer.
    This endpoint only renders images.
    """
    try:
        payload = parse_payload_from_request()

        if payload.get("kind") == "text":
            entries = (
                payload.get("text_entries")
                or payload.get("texts")
                or []
            )

            # Preview must NEVER generate missing codes.
            # Manual codes are preserved; an empty code stays empty.
            if isinstance(entries, list) and entries:
                prepared = prepare_text_serials(
                    entries,
                    generate_missing=False
                )
                payload["text_entries"] = prepared

        files = request.files

        images = build_images_for_request(
            payload,
            files
        )

        previews = [
            image_to_data_url(img)
            for img in images[:5]
        ]

        return jsonify({
            "ok": True,
            "previews": previews,
            "count": len(images)
        })

    except Exception as e:
        traceback.print_exc()

        return jsonify({
            "ok": False,
            "error": str(e)
        }), 400


@app.route("/api/print", methods=["POST"])
def api_print():
    try:
        # IMPORTANT:
        # This check is only performed when PRINT is requested.
        # Preview does not touch the printer.
        #
        # For file:// URIs a missing node fails fast with a precise
        # message. For usb:// URIs we do NOT pre-fail on a negative
        # probe: a sleeping/autosuspended printer often answers only
        # after the retry loop's wake sequence, so the print attempt
        # itself (with retries + fallbacks) is the real test.
        # For tcp:// (Wi-Fi) URIs we DO pre-probe: a dead host would
        # otherwise burn the whole retry budget on TCP timeouts.
        primary_uri = active_printer_uri()
        if (
            primary_uri.startswith("file://")
            and not printer_device_present()
            and not PRINTER_FALLBACKS
        ):
            device = printer_device_path()

            raise RuntimeError(
                f"Brother printer device not found: {device}\n\n"
                "Preview works without the printer, but printing "
                "requires the printer device to be available.\n\n"
                "If Flask is running in Docker, start the container "
                "with the printer device mapped, for example:\n"
                "  --device=/dev/usb/lp0:/dev/usb/lp0\n"
                "  --device=/dev/bus/usb:/dev/bus/usb\n\n"
                "Or set PRINTER to the correct Brother-QL printer URI "
                "(e.g. usb://0x04f9:0x209b)."
            )

        if connection_type_of(primary_uri) == "network":
            probe = probe_printer_uri(primary_uri)
            if not probe.get("present"):
                hint = _troubleshooting_hint(
                    probe.get("error", ""), [primary_uri]
                )
                raise RuntimeError(
                    f"Network printer unreachable: "
                    f"{probe.get('error', 'unknown error')}\n\n{hint}"
                )

        payload = parse_payload_from_request()

        if payload.get("kind") == "text":
            entries = (
                payload.get("text_entries")
                or payload.get("texts")
                or []
            )

            # Print must NEVER generate missing codes.
            # Manual codes are preserved; an empty code stays empty.
            prepared_entries = prepare_text_serials(
                entries,
                generate_missing=False
            )

            payload["text_entries"] = prepared_entries

        files = request.files

        images = build_images_for_request(
            payload,
            files
        )

        label_size = payload.get(
            "label_size",
            DEFAULT_LABEL_SIZE
        )

        result = print_images(
            images,
            label_size
        ) or {}

        if payload.get("kind") == "text":
            copies = max(
                1,
                int(payload.get("copies", 1))
            )

            for entry in payload.get(
                "text_entries",
                []
            ):
                if isinstance(entry, dict):
                    serial = entry.get(
                        "serial"
                    )

                    if serial:
                        mark_serial_printed(
                            serial,
                            entry.get("text"),
                            copies
                        )

        elif payload.get("serial"):
            mark_serial_printed(
                payload["serial"]
            )

        response = {
            "ok": True,
            "printed": len(images),
        }
        if isinstance(result, dict):
            if result.get("uri"):
                response["printer_used"] = result["uri"]
            if result.get("warning"):
                response["warning"] = result["warning"]
        return jsonify(response)

    except Exception as e:
        traceback.print_exc()

        return jsonify({
            "ok": False,
            "error": str(e)
        }), 400


@app.route("/api/health")
def api_health():
    """Liveness probe for Docker HEALTHCHECK / uptime monitors.

    Never touches the printer or the database; just proves the web app
    itself is up.
    """
    return jsonify({
        "ok": True,
        "app": APP_NAME,
        "version": APP_VERSION,
    })


@app.route("/api/status")
def api_status():
    try:
        snapshot = probe_printer()
    except Exception as e:
        traceback.print_exc()
        return jsonify({
            "ok": True,
            "app": APP_NAME,
            "version": APP_VERSION,
            "model": active_model(),
            "printer_display_name": active_display_name(),
            "printer": active_printer_uri(),
            "device_present": False,
            "device_path": printer_device_path(),
            "detail": f"Status probe failed: {e}",
        })
    # Keep the historic flat keys for backwards compatibility and add
    # the full snapshot alongside.
    payload = {
        "ok": True,
        "app": APP_NAME,
        "version": APP_VERSION,
        "model": snapshot.get("model"),
        "printer_display_name": snapshot.get("printer_display_name"),
        "printer": snapshot.get("printer"),
        "device_present": snapshot.get("device_present"),
        "device_path": snapshot.get("device_path"),
    }
    payload.update(snapshot)
    payload["ok"] = True
    return jsonify(payload)


@app.route("/api/printer/diagnostics")
def api_printer_diagnostics():
    """Live USB/kernel visibility + config, for troubleshooting."""
    try:
        snapshot = probe_printer()
        effective = get_printer_config()
        snapshot["config"] = {
            "model": effective["model"],
            "printer": effective["uri"],
            "display_name": effective["display_name"],
            "sources": effective["sources"],
            "env_defaults": effective["env_defaults"],
            "fallbacks_configured": PRINTER_FALLBACKS,
            "auto_fallback": PRINTER_AUTO_FALLBACK,
            "max_retries": PRINT_MAX_RETRIES,
            "retry_delay": PRINT_RETRY_DELAY,
            "wake_wait": PRINT_WAKE_WAIT,
            "status_timeout": PRINT_STATUS_TIMEOUT,
        }
        snapshot["ok"] = True
        return jsonify(snapshot)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/printer/reconnect", methods=["POST"])
def api_printer_reconnect():
    """Probe the printer and optionally issue a USB reset.

    Body (JSON, optional): {"reset": true} to attempt a USB port reset
    before re-probing. Preview never calls this; it is purely a
    recovery helper for the Print path.
    """
    try:
        payload = request.get_json(force=True, silent=True) or {}
        messages = []
        if parse_bool(payload.get("reset")):
            ok, message = try_usb_reset(
                payload.get("printer") or active_printer_uri()
            )
            messages.append(message)
            if not ok:
                snapshot = probe_printer()
                snapshot["ok"] = False
                snapshot["error"] = message
                snapshot["messages"] = messages
                return jsonify(snapshot), 400
            # Give the firmware a moment after reset before probing.
            time.sleep(1.0)
        else:
            _wake_probe(payload.get("printer") or active_printer_uri())
        snapshot = probe_printer()
        snapshot["ok"] = True
        snapshot["messages"] = messages
        return jsonify(snapshot)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9\-.]{0,251}[A-Za-z0-9])?$")


def _validate_printer_config_payload(payload):
    """Validate the Printer panel form into (uri, model, display_name).

    Raises ValueError with a user-friendly message on bad input.
    """
    payload = payload or {}
    connection = str(payload.get("connection", "")).strip().lower()
    if connection not in ("usb", "network"):
        raise ValueError(
            "Connection must be 'usb' or 'network' (Wi-Fi)."
        )

    if connection == "usb":
        uri = str(payload.get("usb_uri", "")).strip()
        if parse_usb_uri(uri) is None:
            raise ValueError(
                f"Invalid USB printer URI '{uri}'. Expected "
                "'usb://0xVVVV:0xPPPP' (e.g. usb://0x04f9:0x209b)."
            )
    else:
        host = str(payload.get("host", "")).strip()
        if not host or not _HOST_RE.match(host):
            raise ValueError(
                f"Invalid printer host '{host}'. Enter the printer's IP "
                "address (e.g. 192.168.1.50) or hostname."
            )
        try:
            port = int(payload.get("port", NETWORK_PRINTER_PORT))
        except (TypeError, ValueError):
            raise ValueError(
                f"Invalid port '{payload.get('port')}'. Use 9100 unless "
                "your printer was configured otherwise."
            )
        if not 1 <= port <= 65535:
            raise ValueError(
                f"Invalid port '{port}'. Use a value between 1 and 65535 "
                "(Brother Wi-Fi printers use 9100)."
            )
        uri = f"tcp://{host}:{port}"

    model = str(payload.get("model", "")).strip()
    if model not in PRINTER_MODEL_IDS:
        raise ValueError(
            f"Unknown model '{model}'. Choose one of: "
            + ", ".join(sorted(PRINTER_MODEL_IDS))
            + "."
        )

    display_name = str(payload.get("display_name", "")).strip()
    return uri, model, display_name


@app.route("/api/printer/config")
def api_printer_config_get():
    """Effective printer config for the Printer settings panel."""
    try:
        config = get_printer_config()
        config["probe"] = probe_printer_uri(config["uri"])
        config["ok"] = True
        return jsonify(config)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/printer/config", methods=["POST"])
def api_printer_config_save():
    """Save the printer connection chosen in the Printer panel.

    Persists to SQLite and takes effect immediately for status,
    diagnostics and subsequent prints.
    """
    try:
        payload = request.get_json(force=True, silent=True) or {}
        uri, model, display_name = _validate_printer_config_payload(payload)

        set_setting("printer_uri", uri)
        set_setting("printer_model", model)
        if display_name:
            set_setting("printer_display_name", display_name)
        else:
            delete_setting("printer_display_name")

        warnings = []
        if connection_type_of(uri) == "network" and not next(
            (m for m in PRINTER_MODELS if m["id"] == model),
            {"wireless": True},
        )["wireless"]:
            warnings.append(
                f"Note: the {model} has no Wi-Fi. Network printing to it "
                "only works through a USB print server. For native Wi-Fi, "
                "use a QL-810W / QL-820NWB / QL-1110NWB."
            )

        config = get_printer_config()
        probe = probe_printer_uri(config["uri"])
        config["probe"] = probe
        config["warnings"] = warnings
        config["ok"] = True
        logger.info(
            "printer config saved: %s (%s, %s)",
            uri, model, config["display_name"],
        )
        return jsonify(config)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/printer/config/reset", methods=["POST"])
def api_printer_config_reset():
    """Clear UI overrides and return to the env-var configuration."""
    try:
        delete_setting("printer_uri")
        delete_setting("printer_model")
        delete_setting("printer_display_name")
        config = get_printer_config()
        config["probe"] = probe_printer_uri(config["uri"])
        config["ok"] = True
        return jsonify(config)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/printer/test", methods=["POST"])
def api_printer_test():
    """Probe a printer URI without saving it (Test button).

    Body: {"uri": "tcp://192.168.1.50:9100"} or a config-style payload
    {"connection": ..., ...} which is validated first.
    """
    try:
        payload = request.get_json(force=True, silent=True) or {}
        uri = str(payload.get("uri", "")).strip()
        if not uri and payload.get("connection"):
            uri, _model, _name = _validate_printer_config_payload(payload)
        if not uri:
            raise ValueError(
                "Provide a printer URI or a connection payload to test."
            )
        result = probe_printer_uri(uri)
        result["ok"] = True
        return jsonify(result)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    init_db()

    port = int(
        os.environ.get(
            "PORT",
            8013
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
