"""The probe attachments must actually SHOW the code they claim to carry.

A probe whose image is unreadable or whose PDF prints nothing is worse than no
probe: it would make every model look modality-incapable. These tests re-derive
each code from the rendered bytes, so a broken renderer fails here rather than
silently poisoning discovery.
"""

from __future__ import annotations

import base64
import re
import struct
import zlib

from clio_agent.providers.model_discovery.probe_assets import (
    _DIGIT_GLYPHS,
    _GLYPH_GAP,
    _GLYPH_HEIGHT,
    _GLYPH_WIDTH,
    _MARGIN,
    _SCALE,
    CODE_DIGITS,
    build_probe_challenge,
    render_code_pdf,
    render_code_png,
)


def _decode_png_code(png_bytes: bytes) -> str:
    """Read the digits back out of the rendered pixels."""

    width, height = struct.unpack(">II", png_bytes[16:24])
    idat = b""
    offset = 8
    while offset < len(png_bytes):
        length = struct.unpack(">I", png_bytes[offset : offset + 4])[0]
        tag = png_bytes[offset + 4 : offset + 8]
        if tag == b"IDAT":
            idat += png_bytes[offset + 8 : offset + 8 + length]
        offset += 12 + length
    raw = zlib.decompress(idat)
    stride = width * 3 + 1
    rows = [raw[i * stride + 1 : (i + 1) * stride] for i in range(height)]

    cells_wide = (width - 2 * _MARGIN) // _SCALE
    digits = (cells_wide + _GLYPH_GAP) // (_GLYPH_WIDTH + _GLYPH_GAP)
    inverse = {glyph: digit for digit, glyph in _DIGIT_GLYPHS.items()}

    def _cell(row: int, column: int) -> str:
        y = _MARGIN + row * _SCALE + _SCALE // 2
        x = _MARGIN + column * _SCALE + _SCALE // 2
        return "#" if rows[y][x * 3] == 0 else "."

    code = ""
    for index in range(digits):
        base = index * (_GLYPH_WIDTH + _GLYPH_GAP)
        glyph = tuple(
            "".join(_cell(row, base + column) for column in range(_GLYPH_WIDTH))
            for row in range(_GLYPH_HEIGHT)
        )
        code += inverse[glyph]
    return code


def test_every_digit_renders_back_to_itself() -> None:
    for digit in _DIGIT_GLYPHS:
        code = digit * CODE_DIGITS
        assert _decode_png_code(render_code_png(code)) == code


def test_a_rendered_png_is_a_valid_png_of_bounded_size() -> None:
    png = render_code_png("1234")
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert png.endswith(b"IEND\xae\x42\x60\x82")
    # Small enough that a probe never approaches the attachment byte ceiling.
    assert len(png) < 16 * 1024


def test_the_pdf_prints_the_code_in_its_content_stream() -> None:
    pdf = render_code_pdf("4821")
    assert pdf.startswith(b"%PDF-1.4")
    assert pdf.rstrip().endswith(b"%%EOF")
    assert re.search(rb"\(4821\)\s*Tj", pdf)
    # A declared /Length that disagreed with the stream would make the PDF
    # unreadable to the model while still "looking" valid to us.
    declared = int(re.search(rb"/Length (\d+)", pdf).group(1))  # type: ignore[union-attr]
    stream = re.search(rb"stream\n(.*?)endstream", pdf, re.DOTALL).group(1)  # type: ignore[union-attr]
    assert declared == len(stream)


def test_the_xref_offsets_point_at_their_objects() -> None:
    """A wrong xref makes the PDF unopenable, which would fail every probe."""

    pdf = render_code_pdf("7777")
    entries = re.findall(rb"^(\d{10}) 00000 n $", pdf, re.MULTILINE)
    assert len(entries) == 5
    for index, entry in enumerate(entries, start=1):
        offset = int(entry)
        assert pdf[offset : offset + 8].startswith(f"{index} 0 obj".encode("ascii"))


def test_each_challenge_mints_fresh_distinct_codes() -> None:
    """Per-probe codes cannot be memorised, and one guess cannot satisfy both."""

    challenges = [build_probe_challenge() for _ in range(20)]
    for challenge in challenges:
        assert challenge.image_code != challenge.pdf_code
        assert len(challenge.image_code) == CODE_DIGITS
        assert _decode_png_code(base64.b64decode(challenge.image_b64)) == challenge.image_code
        assert re.search(
            rb"\(" + challenge.pdf_code.encode("ascii") + rb"\)\s*Tj",
            base64.b64decode(challenge.pdf_b64),
        )
    # Not a constant: twenty draws must not all be the same code.
    assert len({challenge.image_code for challenge in challenges}) > 1
