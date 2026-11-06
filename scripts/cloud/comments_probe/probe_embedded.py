"""Phase 0 probe: name the embedded picture under a region on a rendition page.

Run ``make_fixtures.py OUT_DIR`` and convert both files to PDF first, then:

    uv run --no-sync python -I scripts/cloud/comments_probe/probe_embedded.py OUT_DIR

PowerPoint: rendition page N is slide N, and a shape's box in EMU, divided by the slide
size, is in the same normalized coordinates as a region on that page. Word: inline
pictures have no absolute position, so the probe reads where the rendition draws each
image (the content stream's ``cm`` before ``Do``) and the pictures in document order.
In both, the picture's package bytes hash to the workspace PNG it was made from.
"""

import hashlib
import sys

from docx import Document
from PIL import Image
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pypdf import PdfReader
from pypdf.generic import ContentStream

Box = tuple[float, float, float, float]


def short_sha(data: bytes) -> str:
    """The first 16 hex digits of the SHA-256 of ``data``."""
    return hashlib.sha256(data).hexdigest()[:16]


def overlaps(a: Box, b: Box) -> bool:
    """Whether two normalized ``(x, y, width, height)`` boxes intersect."""
    return not (
        a[0] > b[0] + b[2] or a[0] + a[2] < b[0] or a[1] > b[1] + b[3] or a[1] + a[3] < b[1]
    )


def multiply(m: list[float], n: list[float]) -> list[float]:
    """Compose two PDF transformation matrices ``[a b c d e f]``."""
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return [
        a * a2 + b * c2,
        a * b2 + b * d2,
        c * a2 + d * c2,
        c * b2 + d * d2,
        e * a2 + f * c2 + e2,
        e * b2 + f * d2 + f2,
    ]


def pptx_pictures(folder: str, region: Box) -> None:
    """Print each picture shape with its normalized box and whether ``region`` covers it."""
    deck = Presentation(f"{folder}/deck.pptx")
    width, height = deck.slide_width, deck.slide_height
    for index, slide in enumerate(deck.slides):
        for shape in slide.shapes:
            if shape.shape_type != MSO_SHAPE_TYPE.PICTURE:
                continue
            box = (
                shape.left / width,
                shape.top / height,
                shape.width / width,
                shape.height / height,
            )
            print(
                f"pptx slide {index + 1} shape id={shape.shape_id} name={shape.name!r} "
                f"box={tuple(round(v, 3) for v in box)} blob={short_sha(shape.image.blob)} "
                f"hit={overlaps(box, region)}"
            )
    page = PdfReader(f"{folder}/deck.pdf").pages[3]
    print(
        "deck.pdf page 4 aspect",
        round(float(page.mediabox.width / page.mediabox.height), 4),
        "slide aspect",
        round(width / height, 4),
    )


def docx_pictures(folder: str) -> None:
    """Print the Word file's pictures in document order with their package bytes' hash."""
    report = Document(f"{folder}/report.docx")
    embed = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
    rows = []
    for index, paragraph in enumerate(report.paragraphs):
        for blip in paragraph._p.xpath(".//a:blip"):
            part = report.part.related_parts[blip.get(embed)]
            rows.append((index, part.partname, short_sha(part.blob)))
    print("docx pictures (paragraph, part, sha):", rows)


def pdf_image_boxes(path: str) -> None:
    """Print every image each page draws, with pixel size and normalized top-left box."""
    reader = PdfReader(path)
    for number, page in enumerate(reader.pages, start=1):
        width, height = float(page.mediabox.width), float(page.mediabox.height)
        xobjects = page.get("/Resources", {}).get("/XObject", {}) or {}
        stack = [[1.0, 0.0, 0.0, 1.0, 0.0, 0.0]]
        placed = []
        for operands, operator in ContentStream(page.get_contents(), reader).operations:
            if operator == b"q":
                stack.append(list(stack[-1]))
            elif operator == b"Q":
                stack.pop()
            elif operator == b"cm":
                stack[-1] = multiply([float(v) for v in operands], stack[-1])
            elif operator == b"Do":
                xobject = xobjects[operands[0]].get_object()
                if xobject.get("/Subtype") != "/Image":
                    continue
                a, _b, _c, d, e, f = stack[-1]
                placed.append(
                    (
                        operands[0],
                        xobject.get("/Width"),
                        xobject.get("/Height"),
                        round(e / width, 3),
                        round(1 - (f + d) / height, 3),
                        round(a / width, 3),
                        round(d / height, 3),
                    )
                )
        print(
            f"{path.rsplit('/', 1)[-1]} page {number} images (name, px w, px h, x, y, w, h):",
            placed,
        )


def main() -> None:
    folder = sys.argv[1]
    pptx_pictures(folder, region=(0.55, 0.25, 0.4, 0.5))
    docx_pictures(folder)
    pdf_image_boxes(f"{folder}/report.pdf")
    print("chart.png px", Image.open(f"{folder}/chart.png").size)


if __name__ == "__main__":
    main()
