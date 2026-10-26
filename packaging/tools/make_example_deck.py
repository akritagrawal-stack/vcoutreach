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
    2. Redacts sensitive information. Page 2: the small download-starters
       note is painted out, and the old two stat numbers are painted out and
       replaced with the current "10K" and "2,400". The old "25M+" is also
       painted out and replaced by the generic placeholder "XM+" (icons and
       labels are untouched). The replacement text is made of small 8-bit
       alpha masks embedded below as base64 (rendered once with macOS
       CoreGraphics in the system font at the deck's colours and sizes) and
       drawn as image masks on top of the page.
       Covers now carry generic placeholder text (X, Y, Z, W letters only,
       never a real or near-real figure) instead of blanks, so the slides
       still read naturally: "TAM $X", "SAM $Y", the launch targets "X" and
       "Y", "X% of the spread", "Y% to the builder", "Z% for maintenance.",
       "X% of usage", "Raising $X", "X months" and the allocation shares
       "X%", "Y%", "Z%", "W%".
       Pages 5, 6, 7 and 9 have figures covered: the TAM/SAM figures, the
       launch targets, the revenue split and the "cheaper" claim, the raise
       amount, the runway and the allocation percentages. Page 8 is dropped.
       Everything else is shown as is: the "Watch the demo" button (page 4),
       the view count in the page 2 thumbnail and the publikhq.com link
       (page 10). The page 10 "Let's talk" link only shows the words, its
       target (a personal mailto link) lives in a link annotation that is not
       carried over.
       Cover rectangles and patches are burned into the output, so they are
       real redaction.
    3. Writes a new PDF whose pages are only images. The result has no text
       layer, no link annotations, no metadata and no hidden objects.

Dropped page: 8 (founder bios and personal claims). Layout is that of
Publik_Pitch_Deck_2026-09-24.pdf. The bundled example's cover headline was
repainted afterwards to read "Skip software subscriptions." (same typeface and
colors) so the sample carries no profanity. Edit KEEP_PAGES, COVERS and PATCHES below
to process a different deck.
"""
from __future__ import annotations

import base64
import shutil
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

DPI = 130
PAGE_W_PT = 1152.0
PAGE_H_PT = 648.0
KEEP_PAGES = [1, 2, 3, 4, 5, 6, 7, 9, 10]

# Cover rectangles per source page, in raster pixels at DPI (origin top left):
# (x0, y0, x1, y1, (r, g, b) fill with each channel 0..1).
WHITE = (1.0, 1.0, 1.0)
GREY = (0.86, 0.87, 0.90)
TINT = (238 / 255, 241 / 255, 251 / 255)  # page 7 highlighted column
PAD60 = 4  # padding added on every side of a 60 dpi box so no glyph edge shows


def box60(x0, y0, x1, y1, fill=WHITE):
    """Convert a box measured on a 60 dpi preview (960x540) to DPI pixels."""
    s = DPI / 60.0
    return (round(x0 * s), round(y0 * s), round(x1 * s), round(y1 * s), fill)


def pad60(x0, y0, x1, y1, fill=WHITE):
    """box60 with PAD60 added on every side (inputs are tight 60 dpi boxes)."""
    return box60(x0 - PAD60, y0 - PAD60, x1 + PAD60, y1 + PAD60, fill)


COVERS = {
    # Page 2: paint out the old stat numbers (replaced by PATCHES below) and
    # the small download-starters note (stale basis). The 25M+ number, icons
    # and labels stay. The view count in the thumbnail is NOT covered.
    2: [(282, 1012, 482, 1089, WHITE),
        (1700, 1012, 1890, 1089, WHITE),
        (270, 1130, 900, 1170, WHITE),
        # old 25M+ glyphs (replaced by the XM+ placeholder patch)
        (990, 1008, 1215, 1090, WHITE)],
}
# Pages 5, 6, 7 and 9: figures covered (see module docstring). Page 4 and
# page 10 are shown in full. Only the page 10 link target was personal and it
# is not rendered anywhere.
_NEW_COVERS = {
    # Page 5: TAM/SAM figures, bars and footnote, plus two launch targets.
    5: [pad60(38, 118, 482, 212), pad60(547, 130, 640, 165),
        pad60(688, 130, 740, 165)],
    # Page 6: revenue split and the "cheaper" claim.
    6: [pad60(165, 300, 222, 335), pad60(305, 215, 365, 245),
        pad60(305, 400, 362, 432), pad60(305, 443, 330, 460)],
    # Page 7: one usage share (filled with the highlighted column tint).
    7: [pad60(218, 391, 243, 406, TINT)],
    # Page 9: raise amount, runway, allocation percentages and bar. The
    # slide background is white, so WHITE is used throughout.
    9: [pad60(40, 148, 220, 205), pad60(36, 222, 130, 243),
        pad60(40, 342, 78, 445), pad60(40, 318, 253, 328)],
}
for _page, _boxes in _NEW_COVERS.items():
    COVERS.setdefault(_page, []).extend(_boxes)

# Replacement text drawn on top of the page: (mask name, ink left in pt from
# the page left, baseline in pt from the page top, (r, g, b) 0..255).
BLUE = (90, 125, 216)
DARK = (30, 32, 48)  # body text colour of the deck
PATCHES = {
    2: [("10K", 161.0, 593.25, BLUE), ("2,400", 945.5, 593.25, BLUE),
        ("XMplus", 553.00, 593.25, BLUE)],
    5: [("TAMX", 49.50, 172.00, DARK),
        ("SAMY", 49.50, 208.00, DARK),
        ("BetaX", 707.56, 190.00, BLUE),
        ("UsersY", 872.88, 190.00, BLUE)],
    6: [("SpreadX", 417.88, 286.00, DARK),
        ("BuilderY", 415.62, 511.50, DARK),
        ("MaintZ", 378.25, 547.00, DARK)],
    7: [("UsageX", 274.12, 483.50, BLUE)],
    9: [("RaisingX", 49.50, 200.00, DARK),
        ("RunwayX", 44.50, 287.00, BLUE),
        ("AllocX", 73.38, 423.50, DARK),
        ("AllocY", 73.62, 459.00, DARK),
        ("AllocZ", 74.12, 492.50, DARK),
        ("AllocW", 68.88, 531.50, DARK)],
}

PATCH_PX_PER_PT = 8  # mask resolution: 8 raster pixels per PDF point
PATCH_MASKS = {
    "10K": (520, 239, 230.5125, (
        "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"
    )),
    "2,400": (814, 280, 230.5125, (
        "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"
    )),
    "XMplus": (645, 231, 227.0, (
        "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"
    )),
    "TAMX": (602, 158, 141.0, (
        "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"
    )),
    "SAMY": (618, 158, 141.0, (
        "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"
    )),
    "BetaX": (147, 178, 174.0, (
        "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"
        "b5SklrxQWGD4DovmI51lVmbe7F/7SOJJ"
    )),
    "UsersY": (146, 178, 174.0, (
        "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"
    )),
    "SpreadX": (177, 100, 95.0, (
        "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"
    )),
    "BuilderY": (175, 100, 95.0, (
        "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"
    )),
    "MaintZ": (170, 100, 95.0, (
        "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"
        "0+U="
    )),
    "UsageX": (155, 89, 84.0, (
        "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"
        "Jn6uO+PO+PfHP9yef60="
    )),
    "RaisingX": (1090, 239, 191.0, (
        "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"
    )),
    "RunwayX": (553, 109, 103.0, (
        "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"
    )),
    "AllocX": (173, 98, 93.0, (
        "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"
    )),
    "AllocY": (171, 98, 93.0, (
        "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"
    )),
    "AllocZ": (167, 98, 93.0, (
        "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"
        "qLM6+y/YP7CV5i8="
    )),
    "AllocW": (209, 98, 93.0, (
        "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"
    )),
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


def build_pdf(pages: list[tuple[bytes, list[tuple], list[tuple]]]) -> bytes:
    """pages: list of (jpeg bytes, cover rects in pixels, patches).

    Returns PDF bytes.
    """
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    add(b"")  # 1: catalog, filled in below
    add(b"")  # 2: pages tree, filled in below
    page_ids: list[int] = []
    for jpeg, covers, patches in pages:
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
        xobjects = b"/Im0 %d 0 R" % image_id
        for index, (name, left, baseline, (cr, cg, cb)) in enumerate(patches):
            mw, mh, base_row, b64 = PATCH_MASKS[name]
            mask = base64.b64decode("".join(b64))  # already zlib compressed
            mask_id = add(
                b"<< /Type /XObject /Subtype /Image /Width %d /Height %d "
                b"/ColorSpace /DeviceGray /BitsPerComponent 8 "
                b"/Filter /FlateDecode /Length %d >>\nstream\n"
                % (mw, mh, len(mask)) + mask + b"\nendstream"
            )
            solid = zlib.compress(bytes((cr, cg, cb)))
            solid_id = add(
                b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 "
                b"/ColorSpace /DeviceRGB /BitsPerComponent 8 "
                b"/Filter /FlateDecode /SMask %d 0 R /Length %d >>\nstream\n"
                % (mask_id, len(solid)) + solid + b"\nendstream"
            )
            xobjects += b" /Pt%d %d 0 R" % (index, solid_id)
            width_pt = mw / PATCH_PX_PER_PT
            height_pt = mh / PATCH_PX_PER_PT
            bottom = PAGE_H_PT - baseline - (mh - base_row) / PATCH_PX_PER_PT
            ops.append("q %.3f 0 0 %.3f %.3f %.3f cm /Pt%d Do Q"
                       % (width_pt, height_pt, left, bottom, index))
        content = "\n".join(ops).encode("ascii")
        content_id = add(
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"
        )
        page_id = add(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.2f %.2f] "
            b"/Resources << /XObject << %s >> >> /Contents %d 0 R >>"
            % (PAGE_W_PT, PAGE_H_PT, xobjects, content_id)
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
            pages.append((jpeg, COVERS.get(page, []), PATCHES.get(page, [])))
        pdf = build_pdf(pages)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(pdf)
    print(f"wrote {target} ({len(pdf) / 1_000_000:.2f} MB, {len(pages)} pages)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
