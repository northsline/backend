#!/usr/bin/env python3
"""
sticker.py — generate printable device serial stickers.

Produces a PNG image containing:
  - QR code encoding the device serial (16 hex chars)
  - Human-readable serial below it
  - "Northsline Known" label

The image is sized for standard address-label sticker sheets
(roughly 50 x 25 mm at 300 DPI).  Multiple stickers can be
batched into a single A4 PDF for printing.

Usage:
    python sticker.py SERIAL_HEX                    # one-off sticker
    python sticker.py SERIAL_HEX -o output.png      # custom output path
    python sticker.py SERIAL_HEX --pdf output.pdf   # A4 sheet (one sticker, centered)

Designed to be called automatically by flash_known.py after a
successful flash, or standalone for re-printing lost stickers.
"""

import argparse
import os
import sys

import qrcode
from PIL import Image, ImageDraw, ImageFont

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STICKER_DIR = os.path.join(SCRIPT_DIR, "stickers")


def generate_sticker_image(serial_hex: str, size_mm=(50, 25), dpi=300):
    """Render a single sticker as a PIL Image.

    Args:
        serial_hex: 16-char hex serial string.
        size_mm: sticker dimensions in millimetres (w, h).
        dpi: output resolution.

    Returns:
        PIL.Image (RGB).
    """
    w_px = int(size_mm[0] / 25.4 * dpi)
    h_px = int(size_mm[1] / 25.4 * dpi)

    img = Image.new("RGB", (w_px, h_px), "white")
    draw = ImageDraw.Draw(img)

    # --- QR code (left side) ---
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=1,
    )
    qr.add_data(serial_hex.upper())
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").get_image()
    qr_img = qr_img.convert("RGB")

    # Scale QR to fill ~80% of sticker height
    qr_target_h = int(h_px * 0.80)
    qr_target_w = qr_target_h
    qr_img = qr_img.resize((qr_target_w, qr_target_h), Image.NEAREST)

    qr_x = int(2 / 25.4 * dpi)  # ~2mm margin
    qr_y = (h_px - qr_target_h) // 2
    img.paste(qr_img, (qr_x, qr_y))

    # --- Text (right side) ---
    text_x = qr_x + qr_target_w + int(3 / 25.4 * dpi)  # ~3mm gap

    # Try to load a decent font; fall back to default
    font_paths = [
        "/usr/share/fonts/liberation-sans-fonts/LiberationSans-Bold.ttf",
        "/usr/share/fonts/google-carlito-fonts/Carlito-Bold.ttf",
        "/usr/share/fonts/adwaita-sans-fonts/AdwaitaSans-Regular.ttf",
    ]
    font_label = None
    font_serial = None
    for fp in font_paths:
        if os.path.exists(fp):
            font_label = ImageFont.truetype(fp, 18)   # ~18px
            font_serial = ImageFont.truetype(fp, 28)   # ~28px
            break
    if font_label is None:
        font_label = ImageFont.load_default()
        font_serial = font_label

    label_text = "NORTHSLINE / KNOWN"
    serial_text = serial_hex.upper()

    # Draw label
    draw.text((text_x, qr_y + 10), label_text, fill="black", font=font_label)

    # Draw serial (big, monospace-feel)
    draw.text((text_x, qr_y + 45), serial_text, fill="black", font=font_serial)

    # Small footer
    footer_font = None
    for fp in font_paths:
        if os.path.exists(fp):
            footer_font = ImageFont.truetype(fp, 14)
            break
    if footer_font is None:
        footer_font = ImageFont.load_default()
    draw.text((text_x, qr_y + 85), "Scan to verify", fill="gray", font=footer_font)

    return img


def generate_pdf(serial_hex: str, output_path: str):
    """Generate an A4 PDF with a single sticker centered on the page."""
    # A4 at 300 DPI: 2480 x 3508
    a4_w, a4_h = 2480, 3508
    sticker = generate_sticker_image(serial_hex)

    page = Image.new("RGB", (a4_w, a4_h), "white")
    # Center the sticker
    x = (a4_w - sticker.width) // 2
    y = (a4_h - sticker.height) // 2
    page.paste(sticker, (x, y))

    # PIL can't save PDFs with CMYK, but RGB is fine for inkjet/laser
    page.save(output_path, "PDF", resolution=300.0)


def main():
    parser = argparse.ArgumentParser(description="Generate a Known device serial sticker")
    parser.add_argument("serial", help="Device serial number (16 hex chars)")
    parser.add_argument("-o", "--output", default=None,
                        help="Output file path (PNG). Default: stickers/SERIAL.png")
    parser.add_argument("--pdf", metavar="PATH", default=None,
                        help="Also generate an A4 PDF at this path")
    args = parser.parse_args()

    serial = args.serial.strip().lower()

    # Validate
    if len(serial) != 16 or not all(c in "0123456789abcdef" for c in serial):
        print(f"ERROR: serial must be 16 hex chars, got: {serial!r}")
        sys.exit(1)

    os.makedirs(STICKER_DIR, exist_ok=True)

    out_png = args.output or os.path.join(STICKER_DIR, f"{serial}.png")
    img = generate_sticker_image(serial)
    img.save(out_png, "PNG")
    print(f"Sticker image: {out_png}")

    if args.pdf:
        generate_pdf(serial, args.pdf)
        print(f"Sticker PDF:   {args.pdf}")


if __name__ == "__main__":
    main()