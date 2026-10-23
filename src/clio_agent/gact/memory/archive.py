"""Export memory as a ``.zip`` of Markdown documents, and import such an archive.

**Export** lays documents out as ``user/<category>/<slug>.md`` and
``project/<workspace_id>/<category>/<slug>.md``, each the stored document byte for
byte, plus ``manifest.json`` with the CLIO version, the export time and every file's
``sha256``. Versions, proposals and counters are not exported.

**Import** validates the manifest and every document's format, then merges each
document per fact id: a fact in both keeps the newer line (by its date), a fact only in
the archive is added with its original annotation, unless the user asked to forget it
here. A document whose text already
matches is skipped, so importing the same archive twice changes nothing the second
time. Every changed document gets a version authored by the import.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from clio_agent.arc import memory_document as md
from clio_agent.errors import ClioError
from clio_agent.gact.memory.scope import parse_record_name
from clio_agent.gact.memory.service import Author, MemoryService

MANIFEST = "manifest.json"
FORMAT = "clio.memory-export.v1"
_MAX_FILES = 5000
_MAX_BYTES = 50 * 1024 * 1024


class MemoryArchiveError(ClioError):
    """An archive is not a valid CLIO memory export (typed reason)."""

    def __init__(self, problem: str, **details: Any) -> None:
        super().__init__(
            f"invalid memory archive: {problem}",
            error_type="memory_document_invalid",
            details={"problem": problem, **details},
        )


def _clio_version() -> str:
    from importlib.metadata import PackageNotFoundError, version  # noqa: PLC0415

    try:
        return version("clio-agent")
    except PackageNotFoundError:
        return "unknown"


def export_archive(service: MemoryService, scopes: tuple[str, ...]) -> bytes:
    """The ``.zip`` bytes of every document of ``scopes``."""
    files: dict[str, str] = {}
    for stored in service.documents(scopes):
        files[f"{stored.name}.md"] = stored.text
    manifest = {
        "format": FORMAT,
        "clio_version": _clio_version(),
        "exported_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": {
            path: hashlib.sha256(text.encode("utf-8")).hexdigest()
            for path, text in sorted(files.items())
        },
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MANIFEST, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        for path, text in sorted(files.items()):
            archive.writestr(path, text)
    return buffer.getvalue()


def read_archive(data: bytes) -> dict[str, md.MemoryDocument]:
    """Validate an export and return ``{record name: document}``.

    Raises:
        MemoryArchiveError: Not a zip, no or wrong manifest, a hash mismatch, an
            unexpected path, or a document that is not canonical.
    """
    if len(data) > _MAX_BYTES:
        raise MemoryArchiveError("the archive is too large")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise MemoryArchiveError("not a zip file") from None
    with archive:
        names = archive.namelist()
        if MANIFEST not in names:
            raise MemoryArchiveError("manifest.json is missing")
        if len(names) > _MAX_FILES:
            raise MemoryArchiveError("the archive holds too many files")
        try:
            manifest = json.loads(archive.read(MANIFEST))
        except ValueError:
            raise MemoryArchiveError("manifest.json is not JSON") from None
        if manifest.get("format") != FORMAT or not isinstance(manifest.get("files"), dict):
            raise MemoryArchiveError("manifest.json is not a CLIO memory export manifest")
        listed: dict[str, str] = manifest["files"]
        extra = set(names) - set(listed) - {MANIFEST}
        if extra:
            raise MemoryArchiveError("files outside the manifest", files=sorted(extra)[:10])
        documents: dict[str, md.MemoryDocument] = {}
        for path, digest in sorted(listed.items()):
            if path not in names or not path.endswith(".md"):
                raise MemoryArchiveError(f"{path} is listed but missing", path=path)
            raw = archive.read(path)
            if hashlib.sha256(raw).hexdigest() != digest:
                raise MemoryArchiveError(f"{path} does not match its sha256", path=path)
            name = path[: -len(".md")]
            address = parse_record_name(name)
            try:
                document = md.parse(raw.decode("utf-8"))
            except (UnicodeDecodeError, md.MemoryDocumentError) as error:
                raise MemoryArchiveError(f"{path}: {error}", path=path) from None
            if (document.category, document.name) != (address.category, address.slug):
                raise MemoryArchiveError(f"{path} names another document", path=path)
            documents[name] = document
    return documents


def _merge(
    current: md.MemoryDocument, incoming: md.MemoryDocument, forgotten: set[str]
) -> md.MemoryDocument:
    """Per fact id: newer line wins; archive-only facts are added unless forgotten."""
    by_id = {fact.id: fact for fact in current.facts}
    merged = list(current.facts)
    for fact in incoming.facts:
        mine = by_id.get(fact.id)
        if mine is None:
            if fact.id not in forgotten:
                merged.append(fact)
        elif fact.at > mine.at:
            merged[merged.index(mine)] = fact
    kept_forgotten = tuple(dict.fromkeys((*current.forgotten, *incoming.forgotten)))
    aliases = tuple(dict.fromkeys((*current.aliases, *incoming.aliases)))
    return replace(
        current,
        facts=tuple(merged),
        forgotten=kept_forgotten,
        aliases=aliases,
        updated=max(current.updated, incoming.updated),
    )


def import_archive(service: MemoryService, data: bytes, *, import_id: str) -> dict[str, Any]:
    """Merge a CLIO export into memory; report added, updated and skipped documents."""
    documents = read_archive(data)
    author = Author("import", import_id)
    report: dict[str, list[str]] = {"added": [], "updated": [], "skipped": []}
    for name, incoming in documents.items():
        current = service.get(name)
        if current is None:
            service.write(name, incoming, expected_sha=None, author=author)
            report["added"].append(name)
            continue
        merged = _merge(current.document, incoming, set(service.forgotten(current.name.scope)))
        if md.serialize(merged) == current.text:
            report["skipped"].append(name)
            continue
        service.write(name, merged, expected_sha=current.sha, author=author)
        report["updated"].append(name)
    return {
        "import_id": import_id,
        "added": len(report["added"]),
        "updated": len(report["updated"]),
        "skipped": len(report["skipped"]),
        "documents": report,
    }


__all__ = ["FORMAT", "MemoryArchiveError", "export_archive", "import_archive", "read_archive"]
