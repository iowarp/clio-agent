"""Adversarial-review fix round for the workspace resource lane (slice A3).

One test per reviewed finding: media detection order, inline preview
sandboxing, the streamed upload cap, the cancel/refresh race, delivery
honesty, the deletion lifecycle, event-shape parity, a converter that stops
answering, corrupt-index quarantine, the search/structure owner collapse, and
the bounded-name/decode minors.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from clio_agent.gact.resource_custody import ResourceStore
from clio_agent.gact.resource_mime import detect_media_type
from clio_agent.gact.resource_processing import DocumentProcessorClient


def _ooxml_bytes(part_prefix: str) -> bytes:
    """Build a minimal but REAL OOXML package (zip-shaped, stored part names)."""

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("_rels/.rels", "<Relationships/>")
        archive.writestr(f"{part_prefix}document.xml", "<document/>")
    return buffer.getvalue()


def _riff(form: bytes, body: bytes = b"\x00\x00\x00\x00") -> bytes:
    return b"RIFF" + (len(body) + 4).to_bytes(4, "little") + form + body


def _store(tmp_path: Path) -> ResourceStore:
    return ResourceStore(root=tmp_path / "resources", max_resource_bytes=1024 * 1024)


def _ingest(store: ResourceStore, name: str, content: bytes, claimed: str = "") -> object:
    record, _replay = store.create_or_resume(
        workspace_id="ws_mime",
        name=name,
        declared_size=len(content),
        claimed_mime=claimed,
    )
    return store.append(record.id, offset=0, data=content)


# --------------------------------------------------------------------------- #
# Finding 1 — container signatures must not preempt specific evidence
# --------------------------------------------------------------------------- #


def test_ooxml_upload_detects_its_document_type_and_the_real_converter_accepts_it(
    tmp_path: Path,
) -> None:
    """A .docx must not flatten to application/zip, which hid the OOXML branch."""

    store = _store(tmp_path)
    record = _ingest(store, "report.docx", _ooxml_bytes("word/"))

    assert record.detected_mime == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    # The REAL converter mapping, not a test double's lambda.
    assert DocumentProcessorClient("http://processor.test").supports(record) is True


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        ("xl/", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("ppt/", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    ],
)
def test_other_ooxml_packages_detect_their_own_type(prefix: str, expected: str) -> None:
    detected, source = detect_media_type("book" + prefix[:-1], _ooxml_bytes(prefix))
    assert (detected, source) == (expected, "signature")


def test_webp_and_wav_read_the_riff_form_instead_of_inventing_a_type(tmp_path: Path) -> None:
    """RIFF alone is not a media type; the form at bytes 8-12 is the evidence."""

    store = _store(tmp_path)
    webp = _ingest(store, "clipboard.webp", _riff(b"WEBP", b"VP8 \x00\x00\x00\x00"))
    wav = _ingest(store, "clip.wav", _riff(b"WAVE", b"fmt \x00\x00\x00\x00"))

    assert webp.detected_mime == "image/webp"
    assert wav.detected_mime == "audio/wav"
    assert webp.detection_source == "signature"


def test_detection_does_not_consult_the_host_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host whose registry knows nothing must still detect the pinned types."""

    import mimetypes

    monkeypatch.setattr(mimetypes, "guess_type", lambda *args, **kwargs: (None, None))

    assert detect_media_type("notes.md", b"# Notes\n") == ("text/markdown", "utf8_and_extension")
    assert detect_media_type("page.html", b"<html></html>") == ("text/html", "utf8_and_extension")
    assert detect_media_type("plan.docx", b"PK\x03\x04opaque")[0] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


def test_an_unidentifiable_archive_is_still_a_zip() -> None:
    """The generic container signature survives — it is just no longer first."""

    assert detect_media_type("bundle.unknownext", b"PK\x03\x04\x00\x00opaque") == (
        "application/zip",
        "signature",
    )
