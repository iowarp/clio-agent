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
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, get_args

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.parts import Part
from clio_agent.gact.resource_custody import ResourceRecord, ResourceStore
from clio_agent.gact.resource_delivery import DeliveryRepresentation, plan_resource_delivery
from clio_agent.gact.resource_enrichment import describe_resource_parts
from clio_agent.gact.resource_mime import detect_media_type
from clio_agent.gact.resource_processing import (
    DocumentProcessorClient,
    ResourceConverterFactory,
    ResourceCustodyGone,
    ResourceProcessingRecord,
)
from clio_agent.gact.resource_tools import (
    ResourceQueryError,
    list_workspace_resources,
    read_workspace_resource_structure,
)
from clio_agent.gact.types import ModelRef
from tests._config_layer import set_config
from tests.test_gact.test_post_messages import FakeClioAgent

pytestmark = pytest.mark.usefixtures("host_agent_executor")


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


# --------------------------------------------------------------------------- #
# Shared HTTP fixtures
# --------------------------------------------------------------------------- #


def _app(tmp_path: Path) -> Any:
    return build_app(
        sessions_path=tmp_path / "sessions.json",
        agent=FakeClioAgent(answer="unused"),
    )


def _workspace(client: TestClient, root: Path, name: str = "resources") -> str:
    root.mkdir(parents=True, exist_ok=True)
    response = client.post("/v1/workspaces", json={"name": name, "root_path": str(root)})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _upload(
    client: TestClient,
    workspace_id: str,
    *,
    name: str,
    content: bytes,
    media_type: str = "",
) -> dict[str, Any]:
    created = client.post(
        f"/v1/workspaces/{workspace_id}/resources",
        json={"name": name, "size": len(content), "media_type": media_type},
    )
    assert created.status_code == 201, created.text
    resource = created.json()
    if content:
        appended = client.patch(
            resource["upload_url"],
            headers={"Upload-Offset": "0"},
            content=content,
        )
        assert appended.status_code == 204, appended.text
    fetched = client.get(f"/v1/workspaces/{workspace_id}/resources/{resource['id']}")
    assert fetched.status_code == 200, fetched.text
    return dict(fetched.json())


def _session_in(client: TestClient, workspace_id: str) -> str:
    created = client.post("/v1/sessions", json={"title": "res", "workspace_id": workspace_id})
    assert created.status_code == 200, created.text
    return str(created.json()["id"])


def _workspace_events(app: Any, sid: str, event_type: str) -> list[dict[str, Any]]:
    return [
        dict(event.payload)
        for event in app.state.bus.session_events_since(sid, cursor=1)
        if event.type == event_type
    ]


# --------------------------------------------------------------------------- #
# Finding 2 — an uploaded document must not run as same-origin script
# --------------------------------------------------------------------------- #

_SANDBOX_CSP = "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'"


def test_uploaded_html_preview_is_sandboxed_like_the_derivative_sibling(tmp_path: Path) -> None:
    """Serving an upload inline, same-origin, with no CSP is stored XSS."""

    with TestClient(_app(tmp_path)) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        record = _upload(
            client,
            workspace_id,
            name="payload.html",
            content=b"<html><script>fetch('/v1/sessions')</script></html>",
        )
        assert record["detected_mime"] == "text/html"

        preview = client.get(f"/v1/workspaces/{workspace_id}/resources/{record['id']}/preview")

        assert preview.status_code == 200, preview.text
        assert preview.headers["content-security-policy"] == _SANDBOX_CSP
        assert preview.headers["x-content-type-options"] == "nosniff"


def test_uploaded_svg_preview_is_sandboxed(tmp_path: Path) -> None:
    """SVG is an active-content image; it needs the same policy as HTML."""

    with TestClient(_app(tmp_path)) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        record = _upload(
            client,
            workspace_id,
            name="logo.svg",
            content=b'<svg xmlns="http://www.w3.org/2000/svg"><script>1</script></svg>',
        )
        assert record["detected_mime"] == "image/svg+xml"

        preview = client.get(f"/v1/workspaces/{workspace_id}/resources/{record['id']}/preview")

        assert preview.status_code == 200, preview.text
        assert preview.headers["content-security-policy"] == _SANDBOX_CSP


# --------------------------------------------------------------------------- #
# Finding 3 — the chunk cap must bind before the body is buffered
# --------------------------------------------------------------------------- #


def test_a_chunked_upload_body_cannot_outrun_the_chunk_cap(tmp_path: Path) -> None:
    """Without Content-Length the cap was checked only AFTER full buffering."""

    set_config("resources.upload_chunk_bytes", 64)
    with TestClient(_app(tmp_path)) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        created = client.post(
            f"/v1/workspaces/{workspace_id}/resources",
            json={"name": "stream.txt", "size": 4096, "media_type": "text/plain"},
        )
        assert created.status_code == 201, created.text

        def _body() -> Iterator[bytes]:
            for _ in range(32):
                yield b"x" * 64

        response = client.patch(
            created.json()["upload_url"],
            headers={"Upload-Offset": "0"},
            content=_body(),
        )

        assert response.status_code == 413, response.text
        assert response.json()["error"]["error"] == "upload_chunk_too_large"


# --------------------------------------------------------------------------- #
# Finding 7 — resource.ready has ONE payload shape
# --------------------------------------------------------------------------- #


def test_resource_ready_has_the_same_shape_on_both_completion_paths(tmp_path: Path) -> None:
    """A zero-byte create and a final append must publish the same keys."""

    app = _app(tmp_path)
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        sid = _session_in(client, workspace_id)
        assert (
            client.post(
                f"/v1/workspaces/{workspace_id}/resources",
                json={"name": "empty.txt", "size": 0, "media_type": "text/plain"},
            ).status_code
            == 201
        )
        _upload(client, workspace_id, name="filled.txt", content=b"content")

        ready = _workspace_events(app, sid, "resource.ready")

        assert len(ready) == 2
        assert set(ready[0]) == set(ready[1])
        for payload in ready:
            assert payload["upload_url"].endswith("/content")
            assert payload["idempotent_replay"] is False
            assert payload["processing"]["state"] == "not_started"


# --------------------------------------------------------------------------- #
# Converter doubles shared by the lifecycle findings
# --------------------------------------------------------------------------- #

_STRUCTURED_RESULT: dict[str, Any] = {
    "markdown": "# Structured\n",
    "document": {
        "structure": {"texts": [{"text": "Structured"}], "tables": [{"data": [["v"]]}]},
        "capabilities": ["document_structure", "named_derivatives"],
        "warnings": [],
    },
    "derivatives": {
        "schema": "clio.resource-derivatives.v1",
        "entries": [
            {
                "id": "markdown",
                "name": "structured.md",
                "kind": "markdown",
                "media_type": "text/markdown",
                "content": "# Structured\n",
            }
        ],
    },
}


class _MarkdownConverter:
    """Base double: accepts text/markdown, submits a job that stays pending."""

    id = "test-converter"
    priority = 10
    endpoint = "http://processor.test"
    configured = True

    def supports(self, record: Any) -> bool:
        return record.detected_mime == "text/markdown"

    async def submit(self, record: Any, content_path: Path) -> dict[str, Any]:
        del record, content_path
        return {"id": "job_1", "status": "processing"}

    async def status(self, job_id: str) -> dict[str, Any]:
        return {"id": job_id, "status": "processing", "progress": 10}

    async def cancel(self, job_id: str) -> dict[str, Any]:
        return {"id": job_id, "status": "cancelled"}


def _with_converter(tmp_path: Path, converter: Any) -> Any:
    app = _app(tmp_path)
    app.state.resource_converter_factory = ResourceConverterFactory([converter])
    return app


# --------------------------------------------------------------------------- #
# Finding 4 - a status refresh must not clobber a cancel taken during its await
# --------------------------------------------------------------------------- #


def test_a_cancel_during_the_status_await_is_not_overwritten(tmp_path: Path) -> None:
    """The refresh read state, awaited, then persisted unconditionally.

    The cancel is written durably from inside the converter's ``status`` — the
    exact durable effect ``POST .../processing/cancel`` has — so the race is
    real rather than simulated by ordering two requests.
    """

    armed: dict[str, Any] = {}

    class _CancelDuringStatus(_MarkdownConverter):
        async def status(self, job_id: str) -> dict[str, Any]:
            app = armed.get("app")
            if app is None:
                return {"id": job_id, "status": "processing"}
            record = armed["record"]
            current = app.state.resource_processing_store.state(record)
            app.state.resource_processing_store.save_state(
                record,
                current.model_copy(
                    update={
                        "state": "cancelled",
                        "cancellation": {"requested_at": "now", "remote_cancelled": False},
                    }
                ),
            )
            return {"id": job_id, "status": "complete", "result": _STRUCTURED_RESULT}

    app = _with_converter(tmp_path, _CancelDuringStatus())
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        resource = _upload(client, workspace_id, name="doc.md", content=b"# Structured\n")
        base = f"/v1/workspaces/{workspace_id}/resources/{resource['id']}"
        armed["app"] = app
        armed["record"] = app.state.resource_store.get(workspace_id, str(resource["id"]))

        refreshed = client.get(base)

        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["processing"]["state"] == "cancelled"
        # And the completion the poll carried was NOT persisted over it.
        assert client.get(f"{base}/structure").status_code == 409


# --------------------------------------------------------------------------- #
# Finding 5 - a "native" plan must name a lane the delivery path implements
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("media_type", "modality", "expected"),
    [
        ("audio/wav", "audio", "metadata_only"),
        ("video/mp4", "video", "metadata_only"),
    ],
)
def test_a_capable_model_does_not_get_an_unimplemented_native_plan(
    media_type: str, modality: str, expected: str
) -> None:
    """Unsupported native modalities fall back to an honest representation."""

    app = SimpleNamespace(
        state=SimpleNamespace(
            provider_catalog={
                "providers": [
                    {
                        "id": "codex",
                        "health": "ready",
                        "models": [
                            {
                                "model_id": "omni",
                                "availability": "available",
                                "modalities": [modality, "image", "text"],
                                "evidence": {"live": True, "generated_at": "2026-09-01T00:00:00Z"},
                            }
                        ],
                    }
                ]
            }
        )
    )
    record = ResourceRecord(
        id="res_1",
        workspace_id="ws_1",
        name="asset",
        declared_size=1,
        received_size=1,
        detected_mime=media_type,
        state="ready",
    )

    planned = plan_resource_delivery(
        app, resource=record, message_id="m1", model=ModelRef(provider_id="codex", model_id="omni")
    )

    assert planned.representation == expected
    assert planned.reason_code == "native_lane_unimplemented"
    assert modality in planned.reason


def test_pdf_capability_selects_native_pdf_delivery() -> None:
    app = SimpleNamespace(
        state=SimpleNamespace(
            provider_catalog={
                "providers": [
                    {
                        "id": "claude_code",
                        "health": "ready",
                        "models": [
                            {
                                "model_id": "sonnet",
                                "availability": "available",
                                "modalities": ["text", "image", "pdf"],
                                "evidence": {"live": True, "generated_at": "2026-09-03T00:00:00Z"},
                            }
                        ],
                    }
                ]
            }
        )
    )
    record = ResourceRecord(
        id="res_pdf",
        workspace_id="ws_1",
        name="paper.pdf",
        declared_size=10,
        received_size=10,
        detected_mime="application/pdf",
        state="ready",
    )

    planned = plan_resource_delivery(
        app,
        resource=record,
        message_id="m_pdf",
        model=ModelRef(provider_id="claude_code", model_id="sonnet"),
    )

    assert planned.representation == "native"
    assert planned.reason_code == "native_pdf_input"


def test_the_representation_vocabulary_has_no_unproduced_values() -> None:
    """``retrieval`` was never produced and ``sandbox`` was never consumed."""

    assert set(get_args(DeliveryRepresentation)) == {
        "native",
        "bounded_tools",
        "structured_document",
        "metadata_only",
    }


# --------------------------------------------------------------------------- #
# Finding 6 - the deletion lifecycle
# --------------------------------------------------------------------------- #


def test_delete_removes_the_resource_and_announces_it(tmp_path: Path) -> None:
    """There was no coverage at all for DELETE or ``resource.deleted``."""

    app = _app(tmp_path)
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        sid = _session_in(client, workspace_id)
        resource = _upload(client, workspace_id, name="gone.md", content=b"# bye\n")
        root = app.state.resource_store.root / workspace_id / resource["id"]
        assert root.exists()

        deleted = client.delete(f"/v1/workspaces/{workspace_id}/resources/{resource['id']}")

        assert deleted.status_code == 204, deleted.text
        assert not root.exists()
        gone = client.get(f"/v1/workspaces/{workspace_id}/resources/{resource['id']}")
        assert gone.status_code == 404
        announced = _workspace_events(app, sid, "resource.deleted")
        assert [row["id"] for row in announced] == [resource["id"]]
        assert announced[0]["cancellation"]["remote_cancelled"] is False


def test_a_failed_rmtree_keeps_the_record_instead_of_losing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Popping before the delete diverged memory from disk until a restart."""

    app = _app(tmp_path)
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        resource = _upload(client, workspace_id, name="held.md", content=b"# held\n")

        def _held(*_args: Any, **_kwargs: Any) -> None:
            raise PermissionError(13, "file is held open by another reader")

        monkeypatch.setattr("clio_agent.gact.resource_custody.shutil.rmtree", _held)
        response = client.delete(f"/v1/workspaces/{workspace_id}/resources/{resource['id']}")

        assert response.status_code == 409, response.text
        assert response.json()["error"]["error"] == "resource_delete_failed"
        assert response.json()["error"]["details"]["reason"] == "PermissionError"
        # Still authoritative in memory AND still listed.
        assert app.state.resource_store.get(workspace_id, resource["id"]) is not None
        listed = client.get(f"/v1/workspaces/{workspace_id}/resources").json()["resources"]
        assert [row["id"] for row in listed] == [resource["id"]]


def test_processing_state_cannot_resurrect_a_deleted_resource(tmp_path: Path) -> None:
    """A background submit that outlives the DELETE must not re-create custody."""

    app = _app(tmp_path)
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        resource = _upload(client, workspace_id, name="ghost.md", content=b"# ghost\n")
        record = app.state.resource_store.get(workspace_id, resource["id"])
        assert record is not None
        removed = client.delete(f"/v1/workspaces/{workspace_id}/resources/{resource['id']}")
        assert removed.status_code == 204

        with pytest.raises(ResourceCustodyGone):
            app.state.resource_processing_store.save_state(
                record,
                ResourceProcessingRecord(
                    workspace_id=workspace_id,
                    resource_id=record.id,
                    resource_revision=record.revision,
                    source_sha256=record.sha256,
                    state="processing",
                ),
            )
        assert not (app.state.resource_store.root / workspace_id / resource["id"]).exists()


def test_deleting_a_processing_resource_cancels_the_remote_job(tmp_path: Path) -> None:
    cancelled: list[str] = []

    class _Cancellable(_MarkdownConverter):
        async def cancel(self, job_id: str) -> dict[str, Any]:
            cancelled.append(job_id)
            return {"id": job_id, "status": "cancelled"}

    app = _with_converter(tmp_path, _Cancellable())
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        resource = _upload(client, workspace_id, name="busy.md", content=b"# busy\n")
        base = f"/v1/workspaces/{workspace_id}/resources/{resource['id']}"
        assert client.get(base).json()["processing"]["state"] == "processing"

        assert client.delete(base).status_code == 204

        assert cancelled == ["job_1"]


# --------------------------------------------------------------------------- #
# Finding 8 - a converter that stops answering must degrade typed
# --------------------------------------------------------------------------- #


def test_a_converter_that_stops_answering_degrades_typed_and_allows_reprocess(
    tmp_path: Path,
) -> None:
    """The poll used to swallow every transport error, pinning "processing"."""

    # 3, not 2: the upload helper's own readback already spends one poll.
    set_config("resources.status_poll_failure_threshold", 3)
    resubmitted: list[str] = []

    class _VanishingConverter(_MarkdownConverter):
        async def status(self, job_id: str) -> dict[str, Any]:
            raise RuntimeError(f"converter vanished while polling {job_id}")

        async def reprocess(self, record: Any, content_path: Path) -> dict[str, Any]:
            del record, content_path
            resubmitted.append("job_2")
            return {"id": "job_2", "status": "processing"}

    app = _with_converter(tmp_path, _VanishingConverter())
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        sid = _session_in(client, workspace_id)
        resource = _upload(client, workspace_id, name="stuck.md", content=b"# stuck\n")
        base = f"/v1/workspaces/{workspace_id}/resources/{resource['id']}"

        # Under the bound the record stays in flight but COUNTS the failures.
        first = client.get(base).json()["processing"]
        assert first["state"] == "submitted"
        assert first["poll_failures"] == 2

        second = client.get(base).json()["processing"]
        assert second["state"] == "failed"
        assert second["failure"]["code"] == "converter_status_unavailable"
        assert second["failure"]["exception"] == "RuntimeError"
        assert second["failure"]["consecutive_failures"] == 3
        assert _workspace_events(app, sid, "resource.processing_failed")

        # A degraded record is reprocessable; an "in flight" one was not.
        again = client.post(f"{base}/reprocess")
        assert again.status_code == 202, again.text
        assert resubmitted == ["job_2"]


# --------------------------------------------------------------------------- #
# Finding 9 - a corrupt composer index must not take the server down
# --------------------------------------------------------------------------- #


def test_a_corrupt_resource_index_is_quarantined_and_the_server_still_boots(
    tmp_path: Path,
) -> None:
    index = tmp_path / "resources" / "resources.json"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text("{ this is not json", encoding="utf-8")
    ledger = tmp_path / "resource_deliveries.json"
    ledger.write_text("also not json", encoding="utf-8")

    app = build_app(
        sessions_path=tmp_path / "sessions.json",
        agent=FakeClioAgent(answer="unused"),
    )
    with TestClient(app) as client:
        capabilities = client.get("/v1/capabilities").json()["capabilities"]["x_clio_resources"]

        assert capabilities["enabled"] is True
        reasons = {row["kind"]: row for row in capabilities["degradations"]}
        assert set(reasons) == {"resource_custody_index", "resource_delivery_ledger"}
        for row in reasons.values():
            assert row["reason"] == "composer_index_unreadable"
            assert Path(row["quarantined_path"]).exists()
        assert not index.exists()
        # And the service is genuinely usable, not merely alive.
        workspace_id = _workspace(client, tmp_path / "workspace")
        after = _upload(client, workspace_id, name="after.md", content=b"# after\n")
        assert after["state"] == "ready"


# --------------------------------------------------------------------------- #
# Finding 10 - one owner, one readiness gate for search and structure
# --------------------------------------------------------------------------- #


def test_the_structure_tool_serves_derivatives_after_a_failed_refresh(tmp_path: Path) -> None:
    """The route served this state; the tool refused it, so enrichment lied."""

    class _CompleteThenFailingRefresh(_MarkdownConverter):
        async def submit(self, record: Any, content_path: Path) -> dict[str, Any]:
            del record, content_path
            return {"id": "job_1", "status": "complete", "result": _STRUCTURED_RESULT}

        async def reprocess(self, record: Any, content_path: Path) -> dict[str, Any]:
            del record, content_path
            return {"id": "job_2", "status": "processing"}

        async def status(self, job_id: str) -> dict[str, Any]:
            raise RuntimeError(f"refresh failed for {job_id}")

    set_config("resources.status_poll_failure_threshold", 1)
    app = _with_converter(tmp_path, _CompleteThenFailingRefresh())
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        resource = _upload(client, workspace_id, name="doc.md", content=b"# Structured\n")
        base = f"/v1/workspaces/{workspace_id}/resources/{resource['id']}"
        assert client.get(base).json()["processing"]["state"] == "complete"
        # Force a refresh that fails, leaving state=failed with derivatives on disk.
        assert client.post(f"{base}/reprocess").status_code == 202
        assert client.get(base).json()["processing"]["state"] == "failed"

        route = client.get(f"{base}/structure")
        tool = read_workspace_resource_structure(app, workspace_id, str(resource["id"]))

        assert route.status_code == 200, route.text
        assert tool["available"] is True
        assert tool["collections"] == route.json()["collections"]


# --------------------------------------------------------------------------- #
# Finding 11 - the minors
# --------------------------------------------------------------------------- #


def test_a_late_invalid_byte_is_a_typed_refusal_not_a_500(tmp_path: Path) -> None:
    """Only the first 8 KiB are sniffed, so text can turn undecodable later."""

    content = b"a" * 9000 + b"\xff\xfe not utf-8\n"
    app = _app(tmp_path)
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        resource = _upload(client, workspace_id, name="mostly.txt", content=content)
        assert resource["detected_mime"] == "text/plain"

        response = client.get(
            f"/v1/workspaces/{workspace_id}/resources/{resource['id']}/search",
            params={"q": "aaa"},
        )

        assert response.status_code == 415, response.text
        assert response.json()["error"]["error"] == "resource_not_decodable"


def test_a_long_derivative_id_is_stored_under_a_bounded_name(tmp_path: Path) -> None:
    """A 128-character id under the custody tree can cross Windows MAX_PATH."""

    long_id = "table-" + "x" * 110

    class _LongIdConverter(_MarkdownConverter):
        async def submit(self, record: Any, content_path: Path) -> dict[str, Any]:
            del record, content_path
            return {
                "id": "job_1",
                "status": "complete",
                "result": {
                    **_STRUCTURED_RESULT,
                    "derivatives": {
                        "schema": "clio.resource-derivatives.v1",
                        "entries": [
                            {
                                "id": long_id,
                                "name": "wide.md",
                                "kind": "markdown",
                                "media_type": "text/markdown",
                                "content": "# Wide\n",
                            }
                        ],
                    },
                },
            }

    app = _with_converter(tmp_path, _LongIdConverter())
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        resource = _upload(client, workspace_id, name="doc.md", content=b"# Structured\n")
        base = f"/v1/workspaces/{workspace_id}/resources/{resource['id']}"

        listed = client.get(f"{base}/derivatives").json()["derivatives"]

        assert [row["id"] for row in listed] == [long_id]
        record = app.state.resource_store.get(workspace_id, str(resource["id"]))
        assert record is not None
        path, _entry = app.state.resource_processing_store.derivative_path(record, long_id)
        assert path.name != long_id
        assert len(path.name) <= 48
        assert path.read_text(encoding="utf-8") == "# Wide\n"
        content = client.get(f"{base}/derivatives/{long_id}/content")
        assert content.status_code == 200, content.text


# --------------------------------------------------------------------------- #
# Finding 14 - the processor's own warnings must reach the model
# --------------------------------------------------------------------------- #


def test_a_truncated_derivative_manifest_is_announced_in_the_attachment_block(
    tmp_path: Path,
) -> None:
    """The document service reports truncation; the model has to hear it."""

    class _TruncatingConverter(_MarkdownConverter):
        async def submit(self, record: Any, content_path: Path) -> dict[str, Any]:
            del record, content_path
            return {
                "id": "job_1",
                "status": "complete",
                "result": {
                    "markdown": "# Structured\n",
                    "document": {
                        "structure": {"tables": [{"data": [["v"]]}]},
                        "warnings": [
                            {
                                "code": "derivative_entries_truncated",
                                "message": (
                                    "This manifest lists 3 of 900 named views; the remaining "
                                    "structured nodes stay reachable through the document "
                                    "structure by collection and index."
                                ),
                            }
                        ],
                    },
                    "derivatives": {
                        "schema": "clio.resource-derivatives.v1",
                        "entries_truncated": True,
                        "entry_counts": {"included": 3, "available": 900, "omitted": 897},
                        "entries": [
                            {
                                "id": "markdown",
                                "name": "structured.md",
                                "kind": "markdown",
                                "media_type": "text/markdown",
                                "content": "# Structured\n",
                            }
                        ],
                    },
                },
            }

    app = _with_converter(tmp_path, _TruncatingConverter())
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        sid = _session_in(client, workspace_id)
        resource = _upload(client, workspace_id, name="wide.md", content=b"# Structured\n")

        blocks = describe_resource_parts(
            app,
            sid,
            [
                Part(
                    type="resource_ref",
                    resource_id=str(resource["id"]),
                    resource_revision=str(resource["revision"]),
                    name="wide.md",
                )
            ],
        )

        assert len(blocks) == 1
        assert "derivative_entries_truncated" in blocks[0]
        assert "3 of 900" in blocks[0]
        listed = client.get(
            f"/v1/workspaces/{workspace_id}/resources/{resource['id']}/derivatives"
        ).json()
        assert listed["truncated"] is True


# --------------------------------------------------------------------------- #
# Slice A4 sweep - the last two resource bounds are config, not literals
# --------------------------------------------------------------------------- #


def test_the_workspace_resource_listing_cap_is_configurable(tmp_path: Path) -> None:
    """``resources.list_max_records`` bounds the listing and reports truncation.

    Was a bare ``rows[:100]`` literal in ``resource_tools``. **Sabotage:**
    restore the literal -> both rows come back and ``truncated`` stays False.
    """

    set_config("resources.list_max_records", 1)
    app = _app(tmp_path)
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        _upload(client, workspace_id, name="one.md", content=b"# one\n")
        _upload(client, workspace_id, name="two.md", content=b"# two\n")

        listed = list_workspace_resources(app, workspace_id)

    assert len(listed["resources"]) == 1
    assert listed["truncated"] is True


def test_the_structured_node_ceiling_is_configurable(tmp_path: Path) -> None:
    """``resources.structure_node_max_bytes`` bounds ONE structured node.

    Was a bare ``_MAX_NODE_BYTES = 2 * 1024 * 1024`` module literal. The node
    below is ~4 KiB encoded: it passes under the shipped 2 MiB default and must
    be refused once the deployment lowers the ceiling to the 1 KiB floor.
    **Sabotage:** restore the literal -> the refusal never fires and the node
    is served.
    """

    big_text = "x" * 4096
    result = {
        **_STRUCTURED_RESULT,
        "document": {
            **_STRUCTURED_RESULT["document"],
            "structure": {"texts": [{"text": big_text}], "tables": []},
        },
    }

    class _BigNodeConverter(_MarkdownConverter):
        async def submit(self, record: Any, content_path: Path) -> dict[str, Any]:
            del record, content_path
            return {"id": "job_1", "status": "complete", "result": result}

    app = _with_converter(tmp_path, _BigNodeConverter())
    with TestClient(app) as client:
        workspace_id = _workspace(client, tmp_path / "workspace")
        resource = _upload(client, workspace_id, name="big.md", content=b"# big\n")
        resource_id = str(resource["id"])

        # Under the shipped default the node is served...
        served = read_workspace_resource_structure(app, workspace_id, resource_id, "texts", 0)
        assert served["node"]["text"] == big_text

        # ...and the deployment can refuse it by lowering the ceiling.
        set_config("resources.structure_node_max_bytes", 1024)
        with pytest.raises(ResourceQueryError) as excinfo:
            read_workspace_resource_structure(app, workspace_id, resource_id, "texts", 0)

    assert excinfo.value.code == "structure_node_too_large"


def test_converter_activity_window_bounds_are_configuration() -> None:
    """A third-party converter's event volume is a deployment bound, not a constant."""

    from clio_agent.gact.resource_processing import bounded_processing_events

    raw = [
        {"sequence": index, "message": f"stage message {index} " + "y" * 4096, "stage": "s" * 512}
        for index in range(400)
    ]

    # Under the shipped defaults the window is the newest 100 events, each with a
    # bounded message and stage label...
    shipped = bounded_processing_events(raw)
    assert len(shipped) == 100
    assert shipped[0].sequence == 300
    assert len(shipped[0].message) == 1000
    assert len(shipped[0].stage) == 80

    # ...and a deployment that wants a smaller served payload can shrink all three.
    set_config("resources.processing_event_max_records", 5)
    set_config("resources.processing_event_message_chars", 20)
    set_config("resources.processing_event_stage_chars", 4)
    tightened = bounded_processing_events(raw)
    assert len(tightened) == 5
    assert tightened[0].sequence == 395
    assert len(tightened[0].message) == 20
    assert len(tightened[0].stage) == 4

    # A zero/negative bound degrades to a usable floor instead of serving nothing.
    set_config("resources.processing_event_max_records", 0)
    set_config("resources.processing_event_message_chars", -3)
    set_config("resources.processing_event_stage_chars", 0)
    floored = bounded_processing_events(raw)
    assert len(floored) == 1
    assert len(floored[0].message) == 1
    assert len(floored[0].stage) == 1


def test_conversion_poll_cadence_is_configuration() -> None:
    """The wait's poll interval is cadence an operator owns; the budget stays the caller's."""

    from clio_agent.gact.resource_processing_bounds import processing_poll_interval_s

    assert processing_poll_interval_s() == 0.5
    set_config("resources.processing_poll_interval_s", 2.5)
    assert processing_poll_interval_s() == 2.5
    # Never zero: a 0s cadence would spin the status endpoint inside the budget.
    set_config("resources.processing_poll_interval_s", 0)
    assert processing_poll_interval_s() == 0.01
