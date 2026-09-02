"""Deterministic server-side media detection for uploaded resources.

Detection is the SELECTION EVIDENCE for everything downstream: which converter
accepts a resource, whether a preview may be served inline, and which delivery
representation the planner may honestly choose. It therefore has to be both
*specific* and *host-independent*:

* **Specific signatures first**, and a container signature is not specific. A
  bare ``PK\\x03\\x04`` match is not "this is a zip file", it is "this is one of
  the dozens of zip-shaped formats" — OOXML documents, ODF documents and EPUB
  all begin with it. Returning ``application/zip`` for those made every OOXML
  branch downstream unreachable. The same holds for ``RIFF``, whose real type
  lives in the four form bytes at offset 8 (``WEBP`` / ``WAVE`` / ``AVI ``);
  ignoring them mislabelled every WebP — the format a browser produces when a
  user copies an image — as a type that is not even registered.
* **A pinned extension table, never** :func:`mimetypes.guess_type`. The stdlib
  reads the Windows registry (and ``/etc/mime.types``), so the *same upload*
  could detect as ``text/markdown`` on one host and ``application/octet-stream``
  on another, silently changing converter selection and delivery planning with
  the deployment host. The table below is the committed contract instead.

The returned ``detection_source`` is retained as-is by
:class:`~clio_agent.gact.resource_custody.ResourceRecord`, and the record's
``mime_mismatch`` property still compares the CLIENT claim against whatever this
module decided — detection never rewrites the claim, it only out-ranks it.
"""

from __future__ import annotations

from pathlib import PurePosixPath

# Byte-signature evidence that identifies exactly one media type. Container
# signatures are deliberately absent here; they are resolved further down after
# the specific probes and the extension table have had their turn.
_EXACT_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
    (b"\x89HDF\r\n\x1a\n", "application/x-hdf5"),
    (b"CDF\x01", "application/x-netcdf"),
    (b"CDF\x02", "application/x-netcdf"),
    (b"OggS", "application/ogg"),
    (b"fLaC", "audio/flac"),
    (b"ID3", "audio/mpeg"),
    (b"\x1f\x8b", "application/gzip"),
)

# RIFF form types (bytes 8..12). An unlisted form falls through to the extension
# table rather than inventing a media type for it.
_RIFF_FORMS: dict[bytes, str] = {
    b"WEBP": "image/webp",
    b"WAVE": "audio/wav",
    b"AVI ": "video/x-msvideo",
}

# ISO base-media brands (bytes 8..12, when bytes 4..8 are ``ftyp``).
_ISO_BRANDS: dict[bytes, str] = {
    b"isom": "video/mp4",
    b"iso2": "video/mp4",
    b"mp41": "video/mp4",
    b"mp42": "video/mp4",
    b"avc1": "video/mp4",
    b"qt  ": "video/quicktime",
    b"heic": "image/heic",
    b"heix": "image/heic",
    b"mif1": "image/heic",
}

# OOXML part prefixes that appear as stored (uncompressed) local-file-header
# names near the head of the archive, in the order a package lists them.
_OOXML_PARTS: tuple[tuple[bytes, str], ...] = (
    (b"word/", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    (b"xl/", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    (b"ppt/", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
)

# Zip-shaped packages whose declared ``mimetype`` entry (ODF/EPUB) is stored
# first and uncompressed. A zip local file header is 30 fixed bytes, so the
# entry NAME starts at 30 and its stored VALUE immediately follows the name.
_ZIP_LOCAL_HEADER_BYTES = 30
_ODF_MIMETYPE_MARKER = b"mimetype"
_ODF_MIMETYPE_MAX = 96

# The pinned suffix -> media type contract. Every type the converter registry
# (``DocumentProcessorClient._SUPPORTED_MIME_TYPES``) and delivery planning care
# about is covered here so neither depends on the host's registry.
_EXTENSION_TYPES: dict[str, str] = {
    # documents
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".epub": "application/epub+zip",
    ".rtf": "application/rtf",
    # text
    ".txt": "text/plain",
    ".text": "text/plain",
    ".log": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".rst": "text/x-rst",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".html": "text/html",
    ".htm": "text/html",
    ".css": "text/css",
    ".js": "text/javascript",
    ".py": "text/x-python",
    ".sh": "text/x-shellscript",
    ".sql": "text/x-sql",
    ".json": "application/json",
    ".jsonl": "application/json",
    ".xml": "application/xml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".toml": "application/toml",
    ".ini": "text/plain",
    ".cfg": "text/plain",
    ".bib": "text/plain",
    ".tex": "text/x-tex",
    # images
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".svg": "image/svg+xml",
    ".ico": "image/vnd.microsoft.icon",
    ".heic": "image/heic",
    # audio / video
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    # scientific + archives
    ".h5": "application/x-hdf5",
    ".hdf5": "application/x-hdf5",
    ".nc": "application/x-netcdf",
    ".parquet": "application/vnd.apache.parquet",
    ".npy": "application/octet-stream",
    ".zip": "application/zip",
    ".gz": "application/gzip",
    ".tar": "application/x-tar",
    ".bp": "application/octet-stream",
}

# Non-``text/*`` media types whose canonical serialization is UTF-8 text, so an
# upload that decodes cleanly may keep its extension-declared type instead of
# being flattened to ``text/plain``.
_UTF8_COMPATIBLE_TYPES = frozenset(
    {
        "application/json",
        "application/javascript",
        "application/xml",
        "application/yaml",
        "application/x-yaml",
        "application/toml",
        "image/svg+xml",
    }
)

DEFAULT_MEDIA_TYPE = "application/octet-stream"


def _suffix_type(name: str) -> str:
    """Return the pinned media type for ``name``'s suffix, or an empty string."""

    suffix = PurePosixPath(name.replace("\\", "/")).suffix.lower()
    return _EXTENSION_TYPES.get(suffix, "")


def _looks_utf8(head: bytes) -> bool:
    """Return whether ``head`` is a clean UTF-8 prefix of a text document.

    ``head`` is a byte-bounded PREFIX, so a multi-byte character can be split at
    the cut. Retrying with up to three trailing bytes trimmed keeps a legitimate
    UTF-8 upload from being misclassified as binary purely because of where the
    sniff window ended.
    """

    if not head or b"\x00" in head:
        return False
    for trim in range(4):
        candidate = head[: len(head) - trim] if trim else head
        if not candidate:
            return False
        try:
            candidate.decode("utf-8")
        except UnicodeDecodeError:
            continue
        return True
    return False


def _zip_package_type(head: bytes) -> str:
    """Resolve a zip-shaped package to its real type from cheap head evidence."""

    name_start = _ZIP_LOCAL_HEADER_BYTES
    value_start = name_start + len(_ODF_MIMETYPE_MARKER)
    if head[name_start:value_start] == _ODF_MIMETYPE_MARKER:
        raw = head[value_start : value_start + _ODF_MIMETYPE_MAX]
        candidate = "".join(
            chr(byte) for byte in raw if 0x21 <= byte < 0x7F and chr(byte) not in "\"'"
        )
        candidate = candidate.split("PK", 1)[0]
        if candidate.count("/") == 1 and candidate.partition("/")[0] in {
            "application",
            "text",
            "image",
        }:
            return candidate
    if b"[Content_Types].xml" in head or b"_rels/.rels" in head:
        for marker, media_type in _OOXML_PARTS:
            if marker in head:
                return media_type
    return ""


def detect_media_type(name: str, head: bytes) -> tuple[str, str]:
    """Return ``(media_type, detection_source)`` for one upload.

    Args:
        name: The stored display name; only its suffix is consulted.
        head: The leading bytes of the upload (custody reads 8 KiB).

    Returns:
        The detected media type and the evidence that produced it — one of
        ``signature``, ``utf8_and_extension``, ``utf8``, ``extension`` or
        ``fallback``.
    """

    for signature, media_type in _EXACT_SIGNATURES:
        if head.startswith(signature):
            return media_type, "signature"
    if head.startswith(b"RIFF"):
        form = _RIFF_FORMS.get(head[8:12], "")
        if form:
            return form, "signature"
    if head[4:8] == b"ftyp":
        brand = _ISO_BRANDS.get(head[8:12], "")
        if brand:
            return brand, "signature"
    zip_shaped = head.startswith(b"PK\x03\x04")
    if zip_shaped:
        package = _zip_package_type(head)
        if package:
            return package, "signature"
    declared = _suffix_type(name)
    if not zip_shaped and _looks_utf8(head):
        if declared.startswith("text/") or declared in _UTF8_COMPATIBLE_TYPES:
            return declared, "utf8_and_extension"
        return "text/plain", "utf8"
    if declared:
        return declared, "extension"
    if zip_shaped:
        # Last resort for a zip-shaped upload the head could not identify and
        # whose suffix is unknown: the generic container type, never earlier.
        return "application/zip", "signature"
    return DEFAULT_MEDIA_TYPE, "fallback"


__all__ = ["DEFAULT_MEDIA_TYPE", "detect_media_type"]
