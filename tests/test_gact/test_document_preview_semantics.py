"""Preview custody and lineage stay attached to the requested editable output."""

from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.artifacts.transform_edges import detect_used_edges
from clio_agent.gact.artifacts.transforms import record_transform, transform_from_payload
from clio_agent.gact.artifacts.wire import (
    append_turn_child_resource_links,
    append_turn_resource_links,
)
from clio_agent.gact.documents import renditions
from tests.test_gact.test_document_artifacts import _pin, _workspace_session

pytestmark = pytest.mark.usefixtures("host_agent_executor")


def test_preview_lineage_uses_the_immutable_document_and_preserves_its_own_producer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An outer registration cannot claim the PDF or reverse its input edge."""
    root = tmp_path / "workspace"
    root.mkdir()
    source = root / "report.docx"
    source.write_bytes(b"original document")

    def convert(path: Path, output: Path) -> tuple[Path, str]:
        pdf = output / "report.pdf"
        pdf.write_bytes(b"%PDF-1.7\n" + path.read_bytes())
        return pdf, "test-converter"

    monkeypatch.setattr(renditions, "_convert_to_pdf", convert)
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as client:
        app = cast(FastAPI, client.app)
        workspace_id, session_id = _workspace_session(client, root)
        document = _pin(client, session_id, source.name)
        document_id = document["artifact_id"]
        source.write_bytes(b"changed after publication")
        response = client.post(
            f"/v1/artifacts/{document_id}/renditions?session_id={session_id}",
            json={"format": "pdf"},
        )
        assert response.status_code == 200, response.text
        pdf_id = response.json()["artifact"]["artifact_id"]
        registry = get_registry(app)
        original = registry.get_by_artifact_id(document_id)
        preview = registry.get_by_artifact_id(pdf_id)
        assert original is not None and preview is not None
        record_transform(
            app,
            session_id,
            tool_name="create_artifact",
            args={"artifacts": [{"path": str(source)}, {"path": preview[1].path}]},
            call_id="outer-registration",
            ok=True,
            result={},
            minted=[original[1], preview[1]],
            workspace_id=workspace_id,
        )
        graph = client.get(f"/v1/artifacts/{document_id}/lineage").json()
        assert graph["root"] == document_id
        activity = f"activity:{preview[1].producer['call_id']}"
        assert any(
            edge["from"] == document_id and edge["to"] == activity and edge["type"] == "used"
            for edge in graph["edges"]
        )
        assert any(
            edge["from"] == activity and edge["to"] == pdf_id and edge["type"] == "generated"
            for edge in graph["edges"]
        )
        assert not any(edge["from"] == pdf_id and edge["type"] == "used" for edge in graph["edges"])
        assert client.get(f"/v1/artifacts/{pdf_id}/bytes").content == b"%PDF-1.7\noriginal document"
        assert client.get(f"/v1/artifacts/{document_id}/bytes").content == b"original document"
        assert {
            row["name"]
            for row in client.get(f"/v1/workspaces/{workspace_id}/artifacts").json()["artifacts"]
        } == {"report.docx", "report.docx.v1.pdf"}
        ledger = Mock()
        append_turn_resource_links(app, session_id, f"document-rendition:{document_id}", ledger)
        ledger.append_part.assert_not_called()


def test_registration_output_paths_are_not_consumed_inputs(tmp_path: Path) -> None:
    """Explicit used references own registration lineage; file reads still work."""
    root = tmp_path / "workspace"
    root.mkdir()
    source = root / "report.pdf"
    source.write_bytes(b"review pdf")
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as client:
        app = cast(FastAPI, client.app)
        workspace_id, session_id = _workspace_session(client, root)
        _pin(client, session_id, source.name)
        args = {"artifacts": [{"path": str(source), "name": source.name}]}
        scan = detect_used_edges(
            app,
            session_id,
            args=args,
            workspace_id=workspace_id,
            tool_name="artifacts.create_artifact",
            turn_id="",
            trace_id="",
        )
        assert scan.edges == [] and scan.notes == []
        read = detect_used_edges(
            app,
            session_id,
            args={"path": str(source)},
            workspace_id=workspace_id,
            tool_name="read_file",
            turn_id="",
            trace_id="",
        )
        assert len(read.edges) == 1 and read.edges[0].path == str(source)


def test_legacy_registration_projection_discards_output_guesses_and_keeps_declared_sources() -> (
    None
):
    """Old output-path guesses cannot reverse lineage after the registry reloads."""
    from clio_agent.gact.artifacts.transform_types import EdgeEvidence, EdgeRole, ProvEdge

    guessed = ProvEdge(
        role=EdgeRole.USED,
        evidence=EdgeEvidence.HASH_PAIR,
        artifact_id="review_pdf",
        path="report.pdf",
        arg="path",
    )
    source = guessed.model_copy(update={"artifact_id": "source_doc", "arg": "used"})
    record = transform_from_payload(
        {
            "call_id": "registration",
            "instrument": {"tool": "create_artifact"},
            "used": [guessed.model_dump(mode="json"), source.model_dump(mode="json")],
        }
    )
    assert record is not None and record.used == [source]
    assert record.notes == [
        {"reason": "registration_output_refs_not_inputs", "artifact_ids": ["review_pdf"]}
    ]


def test_child_delivery_rollup_keeps_the_editable_document_and_omits_its_preview(
    tmp_path: Path,
) -> None:
    """The child's retained rendition must not reappear as a parent deliverable."""
    from tests.test_gact.test_spawn_runtime_s4 import (
        _FakeTranscript,
        _rollup_app,
        _rollup_mint,
        _rollup_task,
    )

    app = _rollup_app(tmp_path)
    _rollup_task(app, parent_sid="parent", child_sid="child", parent_turn_id="turn")
    document = _rollup_mint(app, "child", "report.docx", "a" * 64)
    preview = _rollup_mint(app, "child", "report.docx.v1.pdf", "b" * 64)
    preview.producer.update(
        designation="document-rendition", source_artifact_id=document.artifact_id
    )
    transcript = _FakeTranscript()
    append_turn_child_resource_links(cast(FastAPI, app), "parent", "turn", transcript)
    assert [part.name for part in transcript.snapshot()] == ["report.docx"]
