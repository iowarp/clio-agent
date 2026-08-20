"""Read native Office and OpenDocument comments without executing document code."""

from __future__ import annotations

import hashlib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree

from clio_agent.gact.documents.models import DocumentAnchor

_MAX_ARCHIVE_ENTRIES = 10_000
_MAX_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 200


class UnsafeDocumentArchiveError(ValueError):
    """An Office/ODF package violates the bounded archive policy."""


@dataclass(frozen=True)
class NativeComment:
    """One native comment extracted from an Office or OpenDocument package."""

    native_id: str
    text: str
    author: str = ""
    anchor: DocumentAnchor = field(default_factory=lambda: DocumentAnchor(profile="native-comment"))
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        """Stable exactly-once identity for this comment text."""

        raw = f"{self.native_id}\0{self.text}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


def extract_native_comments(path: str | Path) -> list[NativeComment]:
    """Extract native comments from supported OOXML and OpenDocument files."""

    source = Path(path)
    suffix = source.suffix.lower()
    if suffix not in {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"}:
        return []
    with zipfile.ZipFile(source) as archive:
        _validate_archive(archive)
        if suffix == ".docx":
            return _word_comments(archive)
        if suffix == ".xlsx":
            return _excel_comments(archive)
        if suffix == ".pptx":
            return _powerpoint_comments(archive)
        return _opendocument_comments(archive)


def _validate_archive(archive: zipfile.ZipFile) -> None:
    infos = archive.infolist()
    if len(infos) > _MAX_ARCHIVE_ENTRIES:
        raise UnsafeDocumentArchiveError("document archive has too many entries")
    total = 0
    for info in infos:
        path = PurePosixPath(info.filename)
        if path.is_absolute() or ".." in path.parts:
            raise UnsafeDocumentArchiveError("document archive contains path traversal")
        total += int(info.file_size)
        if total > _MAX_UNCOMPRESSED_BYTES:
            raise UnsafeDocumentArchiveError("document archive is too large after extraction")
        if info.compress_size == 0:
            if info.file_size > 0:
                raise UnsafeDocumentArchiveError(
                    "document archive has an invalid compression ratio"
                )
            continue
        if info.file_size / info.compress_size > _MAX_COMPRESSION_RATIO:
            raise UnsafeDocumentArchiveError("document archive compression ratio is unsafe")


def _read_xml(archive: zipfile.ZipFile, name: str) -> ElementTree.Element | None:
    try:
        raw = archive.read(name)
    except KeyError:
        return None
    try:
        return ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise UnsafeDocumentArchiveError(f"malformed XML part: {name}") from exc


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attribute(element: ElementTree.Element, name: str) -> str:
    for key, value in element.attrib.items():
        if _local(key) == name:
            return value
    return ""


def _element_text(element: ElementTree.Element) -> str:
    return "".join(element.itertext()).strip()


def _word_comments(archive: zipfile.ZipFile) -> list[NativeComment]:
    root = _read_xml(archive, "word/comments.xml")
    if root is None:
        return []
    comments: list[NativeComment] = []
    for element in root.iter():
        if _local(element.tag) != "comment":
            continue
        native_id = _attribute(element, "id")
        text = _element_text(element)
        if not text:
            continue
        comments.append(
            NativeComment(
                native_id=f"word:{native_id}",
                text=text,
                author=_attribute(element, "author"),
                anchor=DocumentAnchor(
                    profile="native-comment",
                    native_comment_id=f"word:{native_id}",
                    exact=text,
                ),
            )
        )
    return comments


def _excel_comments(archive: zipfile.ZipFile) -> list[NativeComment]:
    comments: list[NativeComment] = []
    for name in sorted(archive.namelist()):
        if not name.startswith("xl/comments") or not name.endswith(".xml"):
            continue
        root = _read_xml(archive, name)
        if root is None:
            continue
        authors = [
            _element_text(element) for element in root.iter() if _local(element.tag) == "author"
        ]
        for index, element in enumerate(
            child for child in root.iter() if _local(child.tag) == "comment"
        ):
            cell = _attribute(element, "ref")
            text = _element_text(element)
            if not text:
                continue
            author_id = _attribute(element, "authorId")
            author = (
                authors[int(author_id)]
                if author_id.isdigit() and int(author_id) < len(authors)
                else ""
            )
            native_id = f"excel:{name}:{cell or index}"
            comments.append(
                NativeComment(
                    native_id=native_id,
                    text=text,
                    author=author,
                    anchor=DocumentAnchor(
                        profile="sheet-range",
                        cell_range=cell,
                        native_comment_id=native_id,
                        exact=text,
                    ),
                    metadata={"part": name},
                )
            )
    return comments


def _powerpoint_comments(archive: zipfile.ZipFile) -> list[NativeComment]:
    comments: list[NativeComment] = []
    for name in sorted(archive.namelist()):
        normalized = name.lower()
        if "/comments/" not in normalized or not normalized.endswith(".xml"):
            continue
        root = _read_xml(archive, name)
        if root is None:
            continue
        for index, element in enumerate(root.iter()):
            if _local(element.tag) not in {"cm", "comment"}:
                continue
            text = _element_text(element)
            if not text:
                continue
            element_id = _attribute(element, "id") or str(index)
            native_id = f"powerpoint:{name}:{element_id}"
            comments.append(
                NativeComment(
                    native_id=native_id,
                    text=text,
                    author=_attribute(element, "authorId"),
                    anchor=DocumentAnchor(
                        profile="native-comment",
                        native_comment_id=native_id,
                        exact=text,
                        source={"part": name},
                    ),
                )
            )
    return comments


def _opendocument_comments(archive: zipfile.ZipFile) -> list[NativeComment]:
    root = _read_xml(archive, "content.xml")
    if root is None:
        return []
    comments: list[NativeComment] = []
    for index, element in enumerate(root.iter()):
        if _local(element.tag) != "annotation":
            continue
        text = _element_text(element)
        if not text:
            continue
        name = _attribute(element, "name") or str(index)
        author = ""
        for child in element.iter():
            if _local(child.tag) == "creator":
                author = _element_text(child)
                break
        native_id = f"opendocument:{name}"
        comments.append(
            NativeComment(
                native_id=native_id,
                text=text,
                author=author,
                anchor=DocumentAnchor(
                    profile="native-comment",
                    native_comment_id=native_id,
                    exact=text,
                ),
            )
        )
    return comments


__all__ = [
    "NativeComment",
    "UnsafeDocumentArchiveError",
    "extract_native_comments",
]
