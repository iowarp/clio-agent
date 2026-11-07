"""Format-aware document viewer routing."""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path

from clio_agent.gact.documents.models import AnchorProfile, DocumentProfile


@dataclass(frozen=True)
class DocumentFormat:
    """One supported document format and its review capabilities."""

    profile: DocumentProfile
    mime_type: str
    anchors: tuple[AnchorProfile, ...]
    native_open: bool
    rendition_formats: tuple[str, ...]


_FORMATS: dict[str, DocumentFormat] = {
    ".md": DocumentFormat("markdown", "text/markdown", ("text-quote",), True, ("pdf",)),
    ".markdown": DocumentFormat("markdown", "text/markdown", ("text-quote",), True, ("pdf",)),
    ".pdf": DocumentFormat(
        "pdf", "application/pdf", ("text-quote", "pdf-quad", "region"), True, ()
    ),
    ".tex": DocumentFormat(
        "latex",
        "application/x-tex",
        ("text-quote", "pdf-quad", "region", "source-map"),
        True,
        ("pdf",),
    ),
    ".html": DocumentFormat(
        "html-static", "text/html", ("text-quote", "region", "dom"), True, ("pdf",)
    ),
    ".htm": DocumentFormat(
        "html-static", "text/html", ("text-quote", "region", "dom"), True, ("pdf",)
    ),
    ".docx": DocumentFormat(
        "ooxml-word",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ("text-quote", "region", "native-comment"),
        True,
        ("pdf",),
    ),
    ".xlsx": DocumentFormat(
        "ooxml-sheet",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ("sheet-range", "region", "native-comment"),
        True,
        ("pdf",),
    ),
    ".pptx": DocumentFormat(
        "ooxml-slides",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ("slide-shape", "region", "native-comment"),
        True,
        ("pdf",),
    ),
    ".odt": DocumentFormat(
        "odf-text",
        "application/vnd.oasis.opendocument.text",
        ("text-quote", "region", "native-comment"),
        True,
        ("pdf",),
    ),
    ".ods": DocumentFormat(
        "odf-sheet",
        "application/vnd.oasis.opendocument.spreadsheet",
        ("sheet-range", "region", "native-comment"),
        True,
        ("pdf",),
    ),
    ".odp": DocumentFormat(
        "odf-slides",
        "application/vnd.oasis.opendocument.presentation",
        ("slide-shape", "region", "native-comment"),
        True,
        ("pdf",),
    ),
    # Images take a dragged region; an SVG also takes picked elements (a legend, an axis,
    # a series). Comments on them are about the image or, when a script made it, the
    # script (comments plan, "One anchor model").
    ".png": DocumentFormat("image", "image/png", ("region",), True, ()),
    ".jpg": DocumentFormat("image", "image/jpeg", ("region",), True, ()),
    ".jpeg": DocumentFormat("image", "image/jpeg", ("region",), True, ()),
    ".webp": DocumentFormat("image", "image/webp", ("region",), True, ()),
    ".gif": DocumentFormat("image", "image/gif", ("region",), True, ()),
    ".svg": DocumentFormat("image", "image/svg+xml", ("region", "dom"), True, ()),
}

_BINARY = DocumentFormat("binary", "application/octet-stream", (), True, ())


def document_format(name: str) -> DocumentFormat:
    """Return the document routing profile for ``name``."""

    suffix = Path(name).suffix.lower()
    known = _FORMATS.get(suffix)
    if known is not None:
        return known
    guessed, _encoding = mimetypes.guess_type(name)
    if guessed and guessed.startswith("text/"):
        return DocumentFormat("binary", guessed, ("text-quote",), True, ())
    return _BINARY


def supported_extensions() -> tuple[str, ...]:
    """Return the stable list of extensions handled by the document surface."""

    return tuple(sorted(_FORMATS))


__all__ = ["DocumentFormat", "document_format", "supported_extensions"]
