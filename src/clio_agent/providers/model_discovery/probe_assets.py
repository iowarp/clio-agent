"""Attachments whose CONTENT a multimodal probe reply must quote back.

A probe that attaches a 1x1 transparent pixel and an empty PDF, then asks the
model to "reply with the single word: ok", cannot tell a model that SAW the
attachments from one whose CLI stripped them before dispatch — both answer
"ok". Every such probe therefore proved only that the CLI accepted the flags.

These assets carry a randomly generated four-digit code rendered INTO the image
and printed INTO the PDF. The reply must contain both codes to evidence both
modalities, and a stripped attachment makes that impossible: the codes are
generated per probe, so they cannot be memorised, and a guess is one in ten
thousand per modality. The two codes are always distinct, so a model that
echoes one for both is not credited twice.

Everything is built from the standard library (``zlib`` + ``struct`` for the
PNG, hand-assembled objects for the PDF) so discovery pulls in no image or PDF
dependency for a probe.
"""

from __future__ import annotations

import base64
import secrets
import struct
import zlib
from dataclasses import dataclass

#: A 5x7 bitmap glyph per digit. Digits (rather than words) because they are the
#: least ambiguous thing to read back: no spelling, no synonym, no case.
_DIGIT_GLYPHS: dict[str, tuple[str, ...]] = {
    "0": (".###.", "#...#", "#..##", "#.#.#", "##..#", "#...#", ".###."),
    "1": ("..#..", ".##..", "..#..", "..#..", "..#..", "..#..", ".###."),
    "2": (".###.", "#...#", "....#", "...#.", "..#..", ".#...", "#####"),
    "3": ("#####", "...#.", "..#..", "...#.", "....#", "#...#", ".###."),
    "4": ("...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."),
    "5": ("#####", "#....", "####.", "....#", "....#", "#...#", ".###."),
    "6": ("..##.", ".#...", "#....", "####.", "#...#", "#...#", ".###."),
    "7": ("#####", "....#", "...#.", "..#..", ".#...", ".#...", ".#..."),
    "8": (".###.", "#...#", "#...#", ".###.", "#...#", "#...#", ".###."),
    "9": (".###.", "#...#", "#...#", ".####", "....#", "...#.", ".##.."),
}

_GLYPH_WIDTH = 5
_GLYPH_HEIGHT = 7
#: Pixels per glyph cell. Large enough that the rendered digits are unambiguous
#: at normal vision-model resolution, small enough that the PNG stays a few KB.
_SCALE = 12
_MARGIN = 12
_GLYPH_GAP = 1

#: How many digits each code carries. Four keeps the reply short while making a
#: blind guess a one-in-ten-thousand event per modality.
CODE_DIGITS = 4


@dataclass(frozen=True)
class ProbeChallenge:
    """One probe's attachments and the codes a genuine reply must quote back."""

    image_code: str
    pdf_code: str
    image_b64: str
    pdf_b64: str


def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def render_code_png(code: str) -> bytes:
    """Render ``code`` as black digits on white in a minimal RGB PNG."""

    cells_wide = len(code) * _GLYPH_WIDTH + (len(code) - 1) * _GLYPH_GAP
    width = cells_wide * _SCALE + 2 * _MARGIN
    height = _GLYPH_HEIGHT * _SCALE + 2 * _MARGIN
    # One byte per cell column, expanded to pixels below; white background.
    cell_rows: list[list[bool]] = [[False] * cells_wide for _ in range(_GLYPH_HEIGHT)]
    cursor = 0
    for digit in code:
        glyph = _DIGIT_GLYPHS[digit]
        for row_index, row in enumerate(glyph):
            for column_index, pixel in enumerate(row):
                if pixel == "#":
                    cell_rows[row_index][cursor + column_index] = True
        cursor += _GLYPH_WIDTH + _GLYPH_GAP

    raw = bytearray()
    blank_row = b"\x00" + b"\xff" * (width * 3)
    for _ in range(_MARGIN):
        raw += blank_row
    for cell_row in cell_rows:
        pixels = bytearray()
        pixels += b"\xff" * (_MARGIN * 3)
        for cell in cell_row:
            pixels += (b"\x00\x00\x00" if cell else b"\xff\xff\xff") * _SCALE
        pixels += b"\xff" * (_MARGIN * 3)
        for _ in range(_SCALE):
            raw += b"\x00" + bytes(pixels)
    for _ in range(_MARGIN):
        raw += blank_row

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _png_chunk(b"IEND", b"")
    )


def render_code_pdf(code: str) -> bytes:
    """Return a valid one-page PDF whose only visible content is ``code``."""

    stream = f"BT /F1 48 Tf 40 60 Td ({code}) Tj ET\n".encode("ascii")
    objects = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 160] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>\nendobj\n",
        b"4 0 obj\n<< /Length "
        + str(len(stream)).encode("ascii")
        + b" >>\nstream\n"
        + stream
        + b"endstream\nendobj\n",
        b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
    ]
    body = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(len(body))
        body.extend(obj)
    xref = len(body)
    body.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    body.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        body.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    body.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode(
            "ascii"
        )
    )
    return bytes(body)


def build_probe_challenge() -> ProbeChallenge:
    """Mint one probe's attachments with fresh, distinct codes.

    Distinct by construction: a reply that echoes the same value for both
    modalities can then only satisfy one of them, so a model that guesses (or a
    CLI that renders one attachment twice) cannot evidence both.
    """

    image_code = f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}"
    pdf_code = image_code
    while pdf_code == image_code:
        pdf_code = f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}"
    return ProbeChallenge(
        image_code=image_code,
        pdf_code=pdf_code,
        image_b64=base64.b64encode(render_code_png(image_code)).decode("ascii"),
        pdf_b64=base64.b64encode(render_code_pdf(pdf_code)).decode("ascii"),
    )


__all__ = [
    "CODE_DIGITS",
    "ProbeChallenge",
    "build_probe_challenge",
    "render_code_pdf",
    "render_code_png",
]
