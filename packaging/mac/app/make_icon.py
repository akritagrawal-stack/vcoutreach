"""Generate the Publikreach ICNS using only the Python standard library.

The mark is a rounded-square tile with a bold letter P and two "reach" arcs in
the upper right, as if the P were pinging outward. The same geometry (in a
64 by 64 box) is used by the in-app inline SVG and the favicon, so keep them in
step when editing. Output is deterministic.
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

TILE = (66, 86, 232)        # brand indigo
GLYPH = (247, 248, 255)     # P
PING = (170, 184, 255)      # reach arcs

# Geometry in a 64 by 64 box (matches the SVG marks in the web UI).
STEM_X0, STEM_X1 = 14.0, 22.0
TOP_Y, BOTTOM_Y = 20.0, 52.0
BOWL_CX, BOWL_CY, BOWL_R = 27.0, 31.0, 11.0
COUNTER_R = 4.0
BOWL_JOIN_Y = BOWL_CY + BOWL_R
ARC_CX, ARC_CY = BOWL_CX, BOWL_CY
ARC_RADII = (17.0, 23.0)
ARC_WIDTH = 3.5
ARC_FROM, ARC_TO = math.radians(-80), math.radians(-10)
TILE_RADIUS = 14.0
MARGIN = 0.0977             # macOS icon grid: 824 of 1024 px


def _box(px, py, x0, y0, x1, y1):
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    dx, dy = abs(px - cx) - (x1 - x0) / 2, abs(py - cy) - (y1 - y0) / 2
    return math.hypot(max(dx, 0.0), max(dy, 0.0)) + min(max(dx, dy), 0.0)


def _tile_sd(px, py):
    cx = cy = 32.0
    dx, dy = abs(px - cx) - (32.0 - TILE_RADIUS), abs(py - cy) - (32.0 - TILE_RADIUS)
    return math.hypot(max(dx, 0.0), max(dy, 0.0)) + min(max(dx, dy), 0.0) - TILE_RADIUS


def _glyph_sd(px, py):
    bowl = min(_box(px, py, STEM_X0, TOP_Y, BOWL_CX, BOWL_JOIN_Y),
               math.hypot(px - BOWL_CX, py - BOWL_CY) - BOWL_R)
    stem = _box(px, py, STEM_X0, TOP_Y, STEM_X1, BOTTOM_Y)
    solid = min(bowl, stem)
    counter = min(_box(px, py, STEM_X1, BOWL_CY - COUNTER_R, BOWL_CX, BOWL_CY + COUNTER_R),
                  math.hypot(px - BOWL_CX, py - BOWL_CY) - COUNTER_R)
    return max(solid, -counter)


def _ping_sd(px, py):
    dx, dy = px - ARC_CX, py - ARC_CY
    radius = math.hypot(dx, dy)
    angle = math.atan2(dy, dx)
    best = 1e9
    for r in ARC_RADII:
        if ARC_FROM <= angle <= ARC_TO:
            d = abs(radius - r)
        else:
            d = min(math.hypot(px - (ARC_CX + r * math.cos(a)), py - (ARC_CY + r * math.sin(a)))
                    for a in (ARC_FROM, ARC_TO))
        best = min(best, d - ARC_WIDTH / 2)
    return best


def _cover(sd, unit):
    return min(1.0, max(0.0, 0.5 - sd / unit))


def png(size):
    scale = (64.0 / (size * (1 - 2 * MARGIN)))   # box units per pixel
    offset = size * MARGIN
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        py = (y + 0.5 - offset) * scale
        for x in range(size):
            px = (x + 0.5 - offset) * scale
            tile = _cover(_tile_sd(px, py), scale)
            if tile <= 0.0:
                raw += b'\0\0\0\0'
                continue
            colour = TILE
            glyph = _cover(_glyph_sd(px, py), scale)
            ping = _cover(_ping_sd(px, py), scale) if size >= 32 else 0.0
            colour = [c + (g - c) * ping for c, g in zip(colour, PING)]
            colour = [c + (g - c) * glyph for c, g in zip(colour, GLYPH)]
            raw += bytes([round(colour[0]), round(colour[1]), round(colour[2]), round(255 * tile)])
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    rows = bytes(raw)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows, 9)) + chunk(b'IEND', b''))


def build_icon(target):
    chunks = []
    for kind, size in ((b'icp4', 16), (b'icp5', 32), (b'icp6', 64), (b'ic07', 128),
                       (b'ic08', 256), (b'ic09', 512), (b'ic10', 1024)):
        content = png(size)
        chunks.append(kind + struct.pack('>I', len(content) + 8) + content)
    body = b''.join(chunks)
    Path(target).write_bytes(b'icns' + struct.pack('>I', len(body) + 8) + body)


if __name__ == '__main__':
    build_icon(Path(__file__).with_name('Publikreach.icns'))
