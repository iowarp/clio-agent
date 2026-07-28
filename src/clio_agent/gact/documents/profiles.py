"""Format-aware document viewer and editor routing."""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path

from clio_agent.gact.documents.models import (
    AnchorProfile,
    DocumentProfile,
    EditorProvider,
)


@dataclass(frozen=True)
class DocumentFormat:
    """One supported document format and its review capabilities."""

    profile: DocumentProfile
    mime_type: str
    anchors: tuple[AnchorProfile, ...]
    native_open: bool
    embedded_editors: tuple[EditorProvider, ...]
    rendition_formats: tuple[str, ...]


_FORMATS: dict[str, DocumentFormat] = {
    ".md": DocumentFormat("markdown", "text/markdown", ("text-quote",), True, (), ("pdf",)),
    ".markdown": DocumentFormat("markdown", "text/markdown", ("text-quote",), True, (), ("pdf",)),
    ".pdf": DocumentFormat("pdf", "application/pdf", ("text-quote", "pdf-quad"), True, (), ()),
    ".tex": DocumentFormat(
        "latex", "application/x-tex", ("text-quote", "source-map"), True, (), ("pdf",)
    ),
    ".html": DocumentFormat("html-static", "text/html", ("text-quote", "dom"), True, (), ("pdf",)),
    ".htm": DocumentFormat("html-static", "text/html", ("text-quote", "dom"), True, (), ("pdf",)),
    ".docx": DocumentFormat(
        "ooxml-word",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ("text-quote", "native-comment"),
        True,
        ("onlyoffice",),
        ("pdf",),
    ),
    ".xlsx": DocumentFormat(
        "ooxml-sheet",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ("sheet-range", "native-comment"),
        True,
        ("onlyoffice",),
        ("pdf",),
    ),
    ".pptx": DocumentFormat(
        "ooxml-slides",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ("slide-shape", "native-comment"),
        True,
        ("onlyoffice",),
        ("pdf",),
    ),
    ".odt": DocumentFormat(
        "odf-text",
        "application/vnd.oasis.opendocument.text",
        ("text-quote", "native-comment"),
        True,
        ("collabora",),
        ("pdf",),
    ),
    ".ods": DocumentFormat(
        "odf-sheet",
        "application/vnd.oasis.opendocument.spreadsheet",
        ("sheet-range", "native-comment"),
        True,
        ("collabora",),
        ("pdf",),
    ),
    ".odp": DocumentFormat(
        "odf-slides",
        "application/vnd.oasis.opendocument.presentation",
        ("slide-shape", "native-comment"),
        True,
        ("collabora",),
        ("pdf",),
    ),
}

_BINARY = DocumentFormat("binary", "application/octet-stream", (), True, (), ())


def document_format(name: str) -> DocumentFormat:
    """Return the document routing profile for ``name``."""

    suffix = Path(name).suffix.lower()
    known = _FORMATS.get(suffix)
    if known is not None:
        return known
    guessed, _encoding = mimetypes.guess_type(name)
    if guessed and guessed.startswith("text/"):
        return DocumentFormat("binary", guessed, ("text-quote",), True, (), ())
    return _BINARY


def supported_extensions() -> tuple[str, ...]:
    """Return the stable list of extensions handled by the document surface."""

    return tuple(sorted(_FORMATS))


__all__ = ["DocumentFormat", "document_format", "supported_extensions"]
