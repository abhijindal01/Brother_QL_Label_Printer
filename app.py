import os
import io
import base64
import glob
import sqlite3
import random
import string
import traceback
import re
from datetime import datetime, timezone

from flask import Flask, request, jsonify, render_template
from PIL import Image, ImageDraw, ImageFont

# Pillow 10 removed the long-deprecated Image.ANTIALIAS constant, but
# brother_ql 0.9.4 (and some versions of python-barcode's ImageWriter) still
# reference it during resize, raising:
#   module 'PIL.Image' has no attribute 'ANTIALIAS'
# Restore the old names as aliases for the modern Resampling enum so those
# libraries keep working without downgrading Pillow.
if not hasattr(Image, "ANTIALIAS"):
    _resampling = getattr(Image, "Resampling", Image)
    Image.ANTIALIAS = _resampling.LANCZOS
    Image.LANCZOS = _resampling.LANCZOS
    Image.BICUBIC = _resampling.BICUBIC
    Image.BILINEAR = _resampling.BILINEAR
    Image.NEAREST = _resampling.NEAREST
import qrcode
import barcode
from barcode.writer import ImageWriter

import time
import usb.core

from brother_ql.raster import BrotherQLRaster
from brother_ql.conversion import convert
from brother_ql.backends.helpers import send as ql_send, guess_backend

PRINT_MAX_RETRIES = 5
PRINT_RETRY_DELAY = 2.5  # seconds (base delay; backs off on each retry)
PRINT_RETRY_BACKOFF = 1.6  # exponential factor applied to the delay
PRINT_RETRY_MAX_DELAY = 12.0  # cap so waking a sleeping printer stays bounded

app = Flask(__name__)

MODEL = os.environ.get("PRINTER_MODEL", "QL-800")

PRINTER = os.environ.get("PRINTER", "usb://0x04f9:0x209b")

DEFAULT_LABEL_SIZE = os.environ.get("DEFAULT_LABEL_SIZE", "62")
SERIAL_DB = os.environ.get("SERIAL_DB", "/app/data/label_serials.db")

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


def _parse_usb_vid_pid(printer_uri):
    """Extract (vendor_id, product_id) ints from a usb:// URI, else None."""
    if not printer_uri.startswith("usb://"):
        return None

    body = printer_uri[len("usb://"):]

    # Strip an optional serial suffix: usb://0x04f9:0x209b/000J...
    body = body.split("/", 1)[0]

    if ":" not in body:
        return None

    vid_str, pid_str = body.split(":", 1)

    try:
        return (int(vid_str, 16), int(pid_str, 16))
    except ValueError:
        return None


def reset_usb_printer():
    """
    Recover a wedged / re-enumerated / just-woken QL printer on the USB bus.

    An "Input/output error" (usb.core.USBError) usually means the kernel's
    usblp driver grabbed the interface, or the printer re-enumerated after a
    previous job or after auto-sleep, leaving brother_ql with a stale handle.
    Resetting the device and detaching the kernel driver clears that state so
    the *next* send attempt starts from a clean handle instead of reusing the
    broken one.

    Best-effort: any failure here is swallowed -- it only exists to improve
    the odds of the following retry succeeding.
    """
    ids = _parse_usb_vid_pid(PRINTER)

    if not ids:
        return

    vid, pid = ids

    try:
        dev = usb.core.find(idVendor=vid, idProduct=pid)

        if dev is None:
            # Not on the bus yet -- likely still re-enumerating after
            # waking from sleep. The retry delay gives it time to appear.
            return

        # Take the interface away from the usblp kernel driver if it grabbed
        # it; this is the #1 cause of the I/O error on Linux.
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except (usb.core.USBError, NotImplementedError):
            pass

        # A bus-level reset forces a clean re-enumeration and drops the
        # stale handle brother_ql would otherwise reuse.
        try:
            dev.reset()
        except usb.core.USBError:
            pass

    except usb.core.USBError:
        # Nothing more we can do here; let the retry loop wait and try again.
        pass


def print_images(images, label_size):
    if not images:
        raise ValueError(
            "There are no labels to print."
        )

    qlr = BrotherQLRaster(MODEL)
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

    last_error = None
    delay = PRINT_RETRY_DELAY

    for attempt in range(1, PRINT_MAX_RETRIES + 1):
        try:
            # Re-resolve the backend fresh on every attempt so a
            # stale/re-enumerated USB handle is never reused.
            backend = guess_backend(PRINTER)

            ql_send(
                instructions=qlr.data,
                printer_identifier=PRINTER,
                backend_identifier=backend,
                blocking=True
            )

            return  # success

        except (usb.core.USBError, ValueError) as e:
            # Retryable, transient conditions on the USB link:
            #   * usb.core.USBError -- includes "Input/output error"
            #     (errno 5) raised when the kernel usblp driver holds the
            #     interface, or when the printer re-enumerated / woke from
            #     sleep and the handle went stale mid-transfer.
            #   * ValueError from brother_ql's pyusb backend, which raises a
            #     plain ValueError ("Device not found" / "Unable to find")
            #     when its own usb.core.find() scan comes up empty during
            #     re-enumeration.
            # Any *other* ValueError (e.g. a bad label size) is a real
            # programming/config error and must surface immediately.
            if isinstance(e, ValueError):
                msg = str(e).lower()
                transient_value_error = (
                    "not found" in msg
                    or "no such device" in msg
                    or "unable to find" in msg
                    or "no backend" in msg
                )

                if not transient_value_error:
                    raise

            last_error = e

            print(
                f"[print_images] Printer error on attempt "
                f"{attempt}/{PRINT_MAX_RETRIES}: {e}"
            )

            if attempt < PRINT_MAX_RETRIES:
                # Actively clear the stuck state (detach usblp, reset the
                # device) BEFORE waiting, so the next attempt gets a clean
                # handle instead of hitting the same I/O error again.
                reset_usb_printer()

                time.sleep(delay)
                delay = min(
                    delay * PRINT_RETRY_BACKOFF,
                    PRINT_RETRY_MAX_DELAY
                )
                continue

    raise RuntimeError(
        "Printer unreachable after "
        f"{PRINT_MAX_RETRIES} attempts: {last_error}\n\n"
        "The QL-800 stopped responding (often 'Input/output error'). "
        "Common causes and fixes:\n"
        "  1. The Linux 'usblp' kernel driver grabbed the printer. "
        "Blacklist it on the host: add 'blacklist usblp' to "
        "/etc/modprobe.d/blacklist-usblp.conf, then 'modprobe -r usblp' "
        "(or reboot).\n"
        "  2. The printer auto-powered-off / went to sleep. Turn it back "
        "on (or disable Auto Power-Off) and print again.\n"
        "  3. The USB device re-enumerated. Check 'lsusb' on the host and "
        "confirm the udev rule for idVendor=04f9, idProduct=209b is in "
        "place, and that the container runs privileged with "
        "/dev/bus/usb mapped."
    ) from last_error


def printer_device_present():
    if not PRINTER.startswith("file://"):
        return True

    device = PRINTER.replace(
        "file://",
        "",
        1
    )

    return os.path.exists(device)


def printer_device_path():
    if PRINTER.startswith("file://"):
        return PRINTER.replace(
            "file://",
            "",
            1
        )

    return PRINTER


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
    return render_template(
        "index.html",
        model=MODEL,
        label_sizes=sorted(
            LABEL_SPECS.keys()
        ),
        fonts=sorted(
            AVAILABLE_FONTS.keys()
        ),
        default_label_size=DEFAULT_LABEL_SIZE
    )


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
        if (
            PRINTER.startswith("file://")
            and not printer_device_present()
        ):
            device = printer_device_path()

            raise RuntimeError(
                f"Brother printer device not found: {device}\n\n"
                "Preview works without the printer, but printing "
                "requires the printer device to be available.\n\n"
                "If Flask is running in Docker, start the container "
                "with the printer device mapped, for example:\n"
                "  --device=/dev/usb/lp0:/dev/usb/lp0\n\n"
                "Or set PRINTER to the correct Brother-QL printer URI."
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

        print_images(
            images,
            label_size
        )

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

        return jsonify({
            "ok": True,
            "printed": len(images)
        })

    except Exception as e:
        traceback.print_exc()

        return jsonify({
            "ok": False,
            "error": str(e)
        }), 400


@app.route("/api/status")
def api_status():
    device_ok = printer_device_present()

    return jsonify({
        "ok": True,
        "model": MODEL,
        "printer": PRINTER,
        "device_present": device_ok,
        "device_path": printer_device_path()
    })


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
