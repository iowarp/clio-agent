"""Document publication retains editable artifacts and version-bound PDF previews."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact import context
from clio_agent.gact.app import build_app
from clio_agent.gact.artifacts.designation_tool import build_create_artifact_tool
from clio_agent.gact.catalog import _builtin_main_agent
from clio_agent.gact.documents import renditions
from clio_agent.gact.documents.profiles import document_format
from tests.test_gact.test_document_artifacts import _pin, _workspace_session

pytestmark = pytest.mark.usefixtures("host_agent_executor")


@pytest.mark.parametrize(
    ("default", "first", "last", "preview_index"),
    [(False, True, None, 0), (True, None, False, 0), (None, False, True, 2)],
)
def test_batch_preview_overrides_remain_bound_to_their_source_after_a_rejected_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    default: bool | None,
    first: bool | None,
    last: bool | None,
    preview_index: int,
) -> None:
    """Mixed batches can publish one preview while retaining other editable sources."""
    root = tmp_path / "workspace"
    root.mkdir()
    sources = [root / "first.docx", root / "last.docx"]
    for source in sources:
        source.write_bytes(source.name.encode())

    def convert(path: Path, output: Path) -> tuple[Path, str]:
        pdf = output / f"{path.stem}.pdf"
        pdf.write_bytes(b"%PDF-1.7\npreview")
        return pdf, "test-converter"

    monkeypatch.setattr(renditions, "_convert_to_pdf", convert)
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as client:
        _, session_id = _workspace_session(client, root)
        app_token = context.set_app(cast(FastAPI, client.app))
        session_token = context.set_session_id(session_id)
        try:
            result = build_create_artifact_tool(_builtin_main_agent()).func(
                artifacts=[
                    {"path": str(sources[0]), "kind": "report", "pdf_preview": first},
                    {"path": str(root / "missing.docx"), "kind": "report"},
                    {"path": str(sources[1]), "kind": "report", "pdf_preview": last},
                ],
                pdf_preview=default,
            )
        finally:
            context.reset(session_token)
            context.reset(app_token)
        assert result["created"] == 2 and result["rejected"] == 1
        assert len(result["pdf_previews"]) == 1
        preview = result["pdf_previews"][0]
        expected = result["artifacts"][preview_index]["artifact_id"]
        assert preview["source_artifact_id"] == expected
        manifest = client.get(f"/v1/artifacts/{expected}/document").json()
        assert manifest["pdf_rendition_artifact_id"] == preview["artifact_id"]
        assert [source.read_bytes() for source in sources] == [b"first.docx", b"last.docx"]


def test_pdf_preview_is_persisted_reused_and_bound_to_immutable_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    source = root / "report.docx"
    source.write_bytes(b"original document")
    seen: list[bytes] = []

    def convert(path: Path, output: Path) -> tuple[Path, str]:
        assert path.is_relative_to(root / ".tmp")
        assert output.is_relative_to(root / ".tmp")
        seen.append(path.read_bytes())
        pdf = output / "report.pdf"
        pdf.write_bytes(b"%PDF-1.7\n" + path.read_bytes())
        return pdf, "test-converter"

    monkeypatch.setattr(renditions, "_convert_to_pdf", convert)
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as client:
        workspace_id, session_id = _workspace_session(client, root)
        first = _pin(client, session_id, source.name)
        source.write_bytes(b"new document")
        endpoint = f"/v1/artifacts/{first['artifact_id']}/renditions?session_id={session_id}"
        preview = client.post(endpoint, json={"format": "pdf"})
        assert preview.status_code == 200, preview.text
        pdf_id = preview.json()["artifact"]["artifact_id"]
        listing = client.get(
            f"/v1/workspaces/{workspace_id}/files?include_hidden=true&exclude_service_storage=true"
        ).json()
        preview_paths = [row["path"] for row in listing["entries"] if row["path"].endswith(".pdf")]
        assert len(preview_paths) == 1
        assert (root / preview_paths[0]).resolve().is_relative_to(root / "artifacts")
        assert (
            client.post(endpoint, json={"format": "pdf"}).json()["artifact"]["artifact_id"]
            == pdf_id
        )
        manifest = client.get(f"/v1/artifacts/{first['artifact_id']}/document").json()
        assert manifest["pdf_rendition_artifact_id"] == pdf_id
        assert client.get(f"/v1/artifacts/{pdf_id}/document/content").content.endswith(
            b"original document"
        )
        second = _pin(client, session_id, source.name)
        assert (
            client.get(f"/v1/artifacts/{second['artifact_id']}/document").json()[
                "pdf_rendition_artifact_id"
            ]
            == ""
        )
        second_preview = client.post(
            f"/v1/artifacts/{second['artifact_id']}/renditions?session_id={session_id}",
            json={"format": "pdf"},
        )
        assert second_preview.status_code == 200, second_preview.text
        assert second_preview.json()["artifact"]["artifact_id"] != pdf_id
    assert seen == [b"original document", b"new document"]
    assert not list((root / ".tmp" / "clio-documents").iterdir())


def test_unavailable_workspace_scratch_keeps_the_accepted_source_artifact(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    source = root / "report.docx"
    source.write_bytes(b"editable source")
    (root / ".tmp").write_text("occupied by an existing file")
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as client:
        _, session_id = _workspace_session(client, root)
        app_token = context.set_app(cast(FastAPI, client.app))
        session_token = context.set_session_id(session_id)
        try:
            result = build_create_artifact_tool(_builtin_main_agent()).func(
                path=str(source), kind="report"
            )
        finally:
            context.reset(session_token)
            context.reset(app_token)
        assert result["created"] == 1
        assert result["pdf_previews"][0]["error"]
        original_id = result["artifacts"][0]["artifact_id"]
        assert (
            client.get(f"/v1/artifacts/{original_id}/document/content").content
            == source.read_bytes()
        )


def test_retained_pdf_restores_an_agent_addressable_workspace_copy_without_reconversion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    source = root / "report.docx"
    source.write_bytes(b"editable source")
    conversions: list[Path] = []

    def convert(path: Path, output: Path) -> tuple[Path, str]:
        conversions.append(path)
        pdf = output / "report.pdf"
        pdf.write_bytes(b"%PDF-1.7\nretained preview")
        return pdf, "test-converter"

    monkeypatch.setattr(renditions, "_convert_to_pdf", convert)
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as client:
        _, session_id = _workspace_session(client, root)
        app_token = context.set_app(cast(FastAPI, client.app))
        session_token = context.set_session_id(session_id)
        try:
            tool = build_create_artifact_tool(_builtin_main_agent()).func
            first = tool(path=str(source), kind="report")["pdf_previews"][0]
            preview_path = Path(first["path"])
            assert preview_path.resolve().is_relative_to(root)
            expected = preview_path.read_bytes()
            preview_path.unlink()
            restored = tool(path=str(source), kind="report")["pdf_previews"][0]
            assert restored["artifact_id"] == first["artifact_id"]
            assert Path(restored["path"]).read_bytes() == expected
        finally:
            context.reset(session_token)
            context.reset(app_token)
    assert len(conversions) == 1


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("preview_option", [None, True])
def test_artifact_tool_publishes_optional_pdf_and_preserves_source_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: bool, preview_option: bool | None
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    source = root / "budget.xlsx"
    source.write_bytes(b"editable workbook")

    def convert(path: Path, output: Path) -> tuple[Path, str]:
        if failure:
            raise renditions.RenditionUnavailableError("LibreOffice unavailable")
        pdf = output / "budget.pdf"
        pdf.write_bytes(b"%PDF-1.7\npreview")
        return pdf, "test-converter"

    monkeypatch.setattr(renditions, "_convert_to_pdf", convert)
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as client:
        _, session_id = _workspace_session(client, root)
        app = cast(FastAPI, client.app)
        app_token = context.set_app(app)
        session_token = context.set_session_id(session_id)
        try:
            result = build_create_artifact_tool(_builtin_main_agent()).func(
                path=str(source), kind="report", pdf_preview=preview_option
            )
        finally:
            context.reset(session_token)
            context.reset(app_token)
        assert result["created"] == 1, result
        from clio_agent.gact.artifacts.registry import get_registry
        from clio_agent.gact.artifacts.wire import mime_for

        original_id = result["artifacts"][0]["artifact_id"]
        found = get_registry(app).get_by_artifact_id(original_id)
        assert found is not None
        record, version = found
        for extension in ("docx", "pptx", "xlsx", "odt", "ods", "odp"):
            name = f"document.{extension}"
            assert mime_for(version, name) == document_format(name).mime_type
        assert (
            client.get(f"/v1/artifacts/{original_id}/document/content").content
            == source.read_bytes()
        )
        preview = result["pdf_previews"][0]
        assert preview["source_artifact_id"] == original_id
        manifest = client.get(f"/v1/artifacts/{original_id}/document").json()
        if failure:
            assert preview["error"] == "LibreOffice unavailable"
            assert manifest["pdf_rendition_artifact_id"] == ""
        else:
            assert Path(preview["path"]).resolve().is_relative_to(root)
            assert Path(preview["path"]).is_file()
            assert manifest["pdf_rendition_artifact_id"] == preview["artifact_id"]
            assert (
                client.get(f"/v1/artifacts/{preview['artifact_id']}/document").json()["profile"]
                == "pdf"
            )
