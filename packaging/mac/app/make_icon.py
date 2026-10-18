"""Generate a small Publik wordmark ICNS using only the Python standard library."""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

# A deliberately simple geometric wordmark, with a P monogram at small sizes.
GLYPHS = {
    'P': ('11110', '10001', '10001', '11110', '10000', '10000', '10000'),
    'U': ('10001', '10001', '10001', '10001', '10001', '10001', '01110'),
    'B': ('11110', '10001', '10001', '11110', '10001', '10001', '11110'),
    'L': ('10000', '10000', '10000', '10000', '10000', '10000', '11111'),
    'I': ('11111', '00100', '00100', '00100', '00100', '00100', '11111'),
    'K': ('10001', '10010', '10100', '11000', '10100', '10010', '10001'),
}


def png(size):
    pixels = bytearray(bytes((20, 25, 36, 255)) * size * size)
    def rectangle(x, y, width, height, color):
        for row in range(max(0, y), min(size, y + height)):
            left, right = max(0, x), min(size, x + width)
            pixels[(row * size + left) * 4:(row * size + right) * 4] = bytes(color) * (right - left)
    word = 'PUBLIK' if size >= 128 else 'P'
    cell = max(1, int(size * 0.76 / (len(word) * 6 - 1)))
    left = (size - (len(word) * 6 - 1) * cell) // 2
    top = (size - 7 * cell) // 2
    for index, letter in enumerate(word):
        for y, row in enumerate(GLYPHS[letter]):
            for x, value in enumerate(row):
                if value == '1':
                    rectangle(left + (index * 6 + x) * cell, top + y * cell,
                              cell, cell, (242, 245, 255, 255))
    rectangle(int(size * .12), int(size * .72), int(size * .76), max(1, size // 32), (142, 157, 255, 255))
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    rows = b''.join(b'\0' + pixels[y * size * 4:(y + 1) * size * 4] for y in range(size))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))


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
