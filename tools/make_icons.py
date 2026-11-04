# SPDX-License-Identifier: AGPL-3.0-or-later
"""Draw the app icons in app/static/icons/ (D124): a stack of three books on deep teal.

Standard library only, so anyone can redraw them: `python tools/make_icons.py`. The shapes sit
inside the central 80% circle, which keeps them clear of the mask Android applies to maskable
icons and of the corners iOS rounds off. Colours are the D116 palette.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "app" / "static" / "icons"
TEAL = (0x12, 0x43, 0x4F)  # --header
PAPER = (0xFB, 0xFA, 0xF7)  # --paper
GOLD = (0xFF, 0xBF, 0x47)  # --focus
SAMPLES = 4  # 4 x 4 samples per pixel for smooth edges

# Books as (left, top, right, bottom, corner radius, colour), in units of the icon's width.
# Each spine has two bands near its ends, so the stack reads as books rather than a menu icon.
BOOKS = [
    (0.23, 0.285, 0.71, 0.425, 0.02, PAPER),
    (0.29, 0.43, 0.77, 0.57, 0.02, GOLD),
    (0.25, 0.575, 0.75, 0.715, 0.02, PAPER),
]
BAND = 0.025  # band width
BAND_INSET = 0.07  # from each end of the spine


def inside_rounded(x: float, y: float, left, top, right, bottom, r) -> bool:
    if not (left <= x <= right and top <= y <= bottom):
        return False
    cx = min(max(x, left + r), right - r)
    cy = min(max(y, top + r), bottom - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


WHITE = (0xFF, 0xFF, 0xFF)
BADGE_ZOOM = 0.62  # the badge shows the books alone, filling most of the square


def badge_at(x: float, y: float):
    """The notification badge (D125): the books in white on transparent, bands cut out.
    Android draws it as a one-colour mask in the status bar."""
    x, y = 0.5 + (x - 0.5) * BADGE_ZOOM, 0.5 + (y - 0.5) * BADGE_ZOOM
    for left, top, right, bottom, r, _ in BOOKS:
        if inside_rounded(x, y, left, top, right, bottom, r):
            for band_left in (left + BAND_INSET, right - BAND_INSET - BAND):
                if band_left <= x <= band_left + BAND:
                    return None
            return WHITE
    return None


def colour_at(x: float, y: float, background_radius: float | None):
    """The colour at (x, y), or None outside the background (transparent)."""
    if background_radius is not None and not inside_rounded(x, y, 0, 0, 1, 1, background_radius):
        return None
    for left, top, right, bottom, r, colour in BOOKS:
        if inside_rounded(x, y, left, top, right, bottom, r):
            for band_left in (left + BAND_INSET, right - BAND_INSET - BAND):
                if band_left <= x <= band_left + BAND:
                    return TEAL
            return colour
    return TEAL


def render(size: int, background_radius: float | None, badge: bool = False) -> bytes:
    """RGBA rows for a PNG: full-bleed when background_radius is None, else a rounded square;
    or the white badge."""
    rows = bytearray()
    step = 1 / (size * SAMPLES)
    for py in range(size):
        rows.append(0)  # filter: none
        for px in range(size):
            r = g = b = a = 0
            for sy in range(SAMPLES):
                for sx in range(SAMPLES):
                    x, y = (px * SAMPLES + sx + 0.5) * step, (py * SAMPLES + sy + 0.5) * step
                    c = badge_at(x, y) if badge else colour_at(x, y, background_radius)
                    if c is not None:
                        r, g, b, a = r + c[0], g + c[1], b + c[2], a + 1
            n = SAMPLES * SAMPLES
            rows += bytes((r // a, g // a, b // a, 255 * a // n) if a else (0, 0, 0, 0))
    return bytes(rows)


def png(size: int, background_radius: float | None, badge: bool = False) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(render(size, background_radius, badge), 9))
        + chunk(b"IEND", b"")
    )


def svg() -> str:
    hexes = {c: "#{:02x}{:02x}{:02x}".format(*c) for c in (TEAL, PAPER, GOLD)}
    books = "".join(
        f'<rect x="{left * 64:g}" y="{top * 64:g}" width="{(right - left) * 64:g}" '
        f'height="{(bottom - top) * 64:g}" rx="{r * 64:g}" fill="{hexes[colour]}"/>'
        + "".join(
            f'<rect x="{x * 64:g}" y="{top * 64:g}" width="{BAND * 64:g}" '
            f'height="{(bottom - top) * 64:g}" fill="{hexes[TEAL]}"/>'
            for x in (left + BAND_INSET, right - BAND_INSET - BAND)
        )
        for left, top, right, bottom, r, colour in BOOKS
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        f'<rect width="64" height="64" rx="14" fill="{hexes[TEAL]}"/>{books}</svg>\n'
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    # Android and desktop: rounded squares with transparent corners; maskable: full-bleed for
    # the launcher's own shape. iOS rounds the corners itself and shows transparency as black.
    files = {
        "icon-192.png": png(192, 0.22),
        "icon-512.png": png(512, 0.22),
        "icon-maskable-512.png": png(512, None),
        "apple-touch-icon.png": png(180, None),
        "icon-badge-96.png": png(96, None, badge=True),  # notifications' status-bar badge (D125)
    }
    for name, data in files.items():
        (OUT / name).write_bytes(data)
    (OUT / "icon.svg").write_bytes(svg().encode())  # LF on Windows too
    print(f"Wrote {len(files) + 1} icons to {OUT}")


if __name__ == "__main__":
    main()
