#!/usr/bin/env python3
"""Build the redacted example pitch deck that ships inside Publikreach.

Usage:
    python packaging/tools/make_example_deck.py SOURCE_PDF [OUTPUT_PDF]

Inputs:
    SOURCE_PDF   The full original deck (10 pages, 16:9). It is only read, never
                 copied into the repository.
    OUTPUT_PDF   Defaults to vcoutreach/data/examples/Publik_Pitch_Deck_Example.pdf

Requirements: poppler's pdftoppm on PATH or at /opt/homebrew/bin/pdftoppm.
Nothing else: the PDF writer below is stdlib only.

What it does:
    1. Rasterizes the kept pages to JPEG with pdftoppm (130 dpi).
    2. Paints solid cover rectangles over the regions that must not ship
       (the audience stat row on page 2, the "Let's talk" link on page 10).
       The cover is burned into the page image, so it is real redaction.
    3. Writes a new PDF whose pages are only those images. The result has no
       text layer, no link annotations, no metadata and no hidden objects.

Dropped pages: 8 (founder bios and personal claims) and 9 (round size, runway,
allocation). Edit KEEP_PAGES and COVERS below to redact a different deck.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DPI = 130
PAGE_W_PT = 1152.0
PAGE_H_PT = 648.0
KEEP_PAGES = [1, 2, 3, 4, 5, 6, 7, 10]

# Cover rectangles per source page, in raster pixels at DPI (origin top left):
# (x0, y0, x1, y1, (r, g, b) fill with each channel 0..1).
WHITE = (1.0, 1.0, 1.0)
OFF_WHITE = (254 / 255, 252 / 255, 248 / 255)  # page 10 background
COVERS = {
    2: [(0, 950, 2080, 1170, WHITE)],
    10: [(850, 985, 1100, 1085, OFF_WHITE)],
}

DEFAULT_OUT = (
    Path(__file__).resolve().parents[2]
    / "vcoutreach" / "data" / "examples" / "Publik_Pitch_Deck_Example.pdf"
)


def find_pdftoppm() -> str:
    found = shutil.which("pdftoppm")
    if found:
        return found
    fallback = "/opt/homebrew/bin/pdftoppm"
    if Path(fallback).exists():
        return fallback
    raise SystemExit("pdftoppm (poppler) is required")


def render_page(pdftoppm: str, source: Path, page: int, workdir: Path) -> Path:
    prefix = workdir / f"p{page:02d}"
    subprocess.run(
        [pdftoppm, "-r", str(DPI), "-jpeg", "-f", str(page), "-l", str(page),
         str(source), str(prefix)],
        check=True, stderr=subprocess.DEVNULL,
    )
    matches = sorted(workdir.glob(f"p{page:02d}-*.jpg"))
    if not matches:
        raise SystemExit(f"pdftoppm produced no image for page {page}")
    return matches[0]


def jpeg_size(data: bytes) -> tuple[int, int]:
    """Return (width, height) from a baseline or progressive JPEG header."""
    i = 2
    while i < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            height = int.from_bytes(data[i + 5:i + 7], "big")
            width = int.from_bytes(data[i + 7:i + 9], "big")
            return width, height
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    raise SystemExit("could not read JPEG dimensions")


def build_pdf(pages: list[tuple[bytes, list[tuple]]]) -> bytes:
    """pages: list of (jpeg bytes, cover rects in pixels). Returns PDF bytes."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    add(b"")  # 1: catalog, filled in below
    add(b"")  # 2: pages tree, filled in below
    page_ids: list[int] = []
    for jpeg, covers in pages:
        width, height = jpeg_size(jpeg)
        scale_x = PAGE_W_PT / width
        scale_y = PAGE_H_PT / height
        image_id = add(
            b"<< /Type /XObject /Subtype /Image /Width %d /Height %d "
            b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode "
            b"/Length %d >>\nstream\n" % (width, height, len(jpeg))
            + jpeg + b"\nendstream"
        )
        ops = ["q %.2f 0 0 %.2f 0 0 cm /Im0 Do Q" % (PAGE_W_PT, PAGE_H_PT)]
        for x0, y0, x1, y1, (r, g, b) in covers:
            px0 = x0 * scale_x
            pw = (x1 - x0) * scale_x
            ph = (y1 - y0) * scale_y
            py0 = PAGE_H_PT - y1 * scale_y
            ops.append("%.3f %.3f %.3f rg %.2f %.2f %.2f %.2f re f"
                       % (r, g, b, px0, py0, pw, ph))
        content = "\n".join(ops).encode("ascii")
        content_id = add(
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"
        )
        page_id = add(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.2f %.2f] "
            b"/Resources << /XObject << /Im0 %d 0 R >> >> /Contents %d 0 R >>"
            % (PAGE_W_PT, PAGE_H_PT, image_id, content_id)
        )
        page_ids.append(page_id)

    kids = b" ".join(b"%d 0 R" % pid for pid in page_ids)
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_ids))

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objects) + 1, xref_at))
    return bytes(out)


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(__doc__)
        return 2
    source = Path(argv[1]).expanduser()
    target = Path(argv[2]).expanduser() if len(argv) == 3 else DEFAULT_OUT
    if not source.is_file():
        raise SystemExit(f"source deck not found: {source}")
    pdftoppm = find_pdftoppm()
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        pages = []
        for page in KEEP_PAGES:
            jpeg = render_page(pdftoppm, source, page, workdir).read_bytes()
            pages.append((jpeg, COVERS.get(page, [])))
        pdf = build_pdf(pages)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(pdf)
    print(f"wrote {target} ({len(pdf) / 1_000_000:.2f} MB, {len(pages)} pages)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
