"""Document artifact protocol, review, working-copy, and editor tests."""

from __future__ import annotations

import hashlib
import os
import time
import zipfile
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.documents import renditions
from clio_agent.gact.documents.editor_callbacks import exact_http_origin
from clio_agent.gact.documents.editors import issue_access_token, verify_access_token
from clio_agent.gact.documents.native_comments import (
    UnsafeDocumentArchiveError,
    extract_native_comments,
)
from clio_agent.gact.documents.profiles import document_format
from clio_agent.gact.documents.store import get_document_store
from clio_agent.gact.loop_inbox import InboxEvent, LoopInbox
from tests.test_gact.test_post_messages import FakeClioAgent

pytestmark = pytest.mark.usefixtures("host_agent_executor")


def _workspace_session(client: TestClient, root: Path) -> tuple[str, str]:
    workspace_id = client.post(
        "/v1/workspaces",
        json={"name": "documents", "root_path": str(root)},
    ).json()["id"]
    session_id = client.post(
        "/v1/sessions",
        json={"workspace_id": workspace_id},
    ).json()["id"]
    return workspace_id, session_id


def _pin(client: TestClient, session_id: str, name: str) -> dict:
    response = client.post(
        f"/v1/sessions/{session_id}/artifacts/pin",
        json={"path": name, "kind": "report"},
    )
    assert response.status_code == 200, response.text
    return response.json()["pinned"]


def _docx(path: Path, comment: str = "") -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")
        if comment:
            archive.writestr(
                "word/comments.xml",
                (
                    '<w:comments xmlns:w="urn:word">'
                    f'<w:comment w:id="1" w:author="Alice"><w:t>{comment}</w:t></w:comment>'
                    "</w:comments>"
                ),
            )


def _odt(path: Path, text: str = "Initial text") -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        archive.writestr(
            "content.xml",
            (
                "<office:document-content "
                'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
                'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
                f"<office:body><office:text><text:p>{text}</text:p></office:text></office:body>"
                "</office:document-content>"
            ),
        )


def test_capability_and_format_profiles_are_explicit(tmp_path: Path) -> None:
    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as client:
        capability = client.get("/v1/capabilities").json()["capabilities"][
            "x_clio_document_artifacts"
        ]

    assert capability["native_comment_trigger"] == "@clio"
    assert capability["static_html_scripts"] == "blocked"
    assert capability["embedded_editors"] == ["onlyoffice", "collabora"]
    assert document_format("report.docx").profile == "ooxml-word"
    assert document_format("model.odp").embedded_editors == ("collabora",)
    assert document_format("paper.tex").anchors == ("text-quote", "source-map")


def test_manifest_and_static_html_content_block_script_execution(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    html = root / "review.html"
    html.write_text("<script>top.location='https://example.com'</script><h1>Review</h1>")

    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as client:
        _workspace_id, session_id = _workspace_session(client, root)
        pinned = _pin(client, session_id, html.name)
        artifact_id = pinned["artifact_id"]

        manifest = client.get(f"/v1/artifacts/{artifact_id}/document")
        content = client.get(f"/v1/artifacts/{artifact_id}/document/content")

    assert manifest.status_code == 200
    assert manifest.json()["profile"] == "html-static"
    assert manifest.json()["anchors"] == ["text-quote", "dom"]
    assert content.status_code == 200
    assert content.headers["content-security-policy"].startswith("sandbox; default-src 'none'")
    assert content.content == html.read_bytes()


def test_review_is_version_bound_dispatched_once_and_preserved_as_typed_part(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    markdown = root / "report.md"
    markdown.write_text("# Finding\nInitial text.\n")
    agent = FakeClioAgent()

    with TestClient(build_app(sessions_path=tmp_path / "sessions.json", agent=agent)) as client:
        _workspace_id, session_id = _workspace_session(client, root)
        pinned = _pin(client, session_id, markdown.name)
        payload = {
            "artifact_id": pinned["artifact_id"],
            "expected_version": pinned["version"],
            "expected_sha256": pinned["sha256"],
            "anchor": {
                "profile": "text-quote",
                "exact": "Initial text.",
                "prefix": "# Finding\n",
            },
            "text": "Make this conclusion more precise.",
            "idempotency_key": "review-idempotency-0001",
        }
        first = client.post(
            f"/v1/sessions/{session_id}/artifact-reviews",
            json=payload,
        )
        second = client.post(
            f"/v1/sessions/{session_id}/artifact-reviews",
            json=payload,
        )
        deadline = time.time() + 5
        while not agent.calls and time.time() < deadline:
            time.sleep(0.02)
        messages = client.get(f"/v1/sessions/{session_id}/messages").json()["messages"]

    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    assert first.json()["id"] == second.json()["id"]
    assert len(agent.calls) == 1
    user_message = next(message for message in messages if message["role"] == "user")
    review_part = next(part for part in user_message["parts"] if part["type"] == "artifact_review")
    assert review_part["artifact_id"] == pinned["artifact_id"]
    assert review_part["artifact_sha256"] == pinned["sha256"]
    assert review_part["review_text"] == "Make this conclusion more precise."
    assert review_part["anchor"]["exact"] == "Initial text."


def test_stale_review_is_rejected_before_dispatch(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    markdown = root / "report.md"
    markdown.write_text("version one")
    agent = FakeClioAgent()

    with TestClient(build_app(sessions_path=tmp_path / "sessions.json", agent=agent)) as client:
        _workspace_id, session_id = _workspace_session(client, root)
        first = _pin(client, session_id, markdown.name)
        markdown.write_text("version two")
        _pin(client, session_id, markdown.name)
        response = client.post(
            f"/v1/sessions/{session_id}/artifact-reviews",
            json={
                "artifact_id": first["artifact_id"],
                "expected_version": first["version"],
                "expected_sha256": first["sha256"],
                "anchor": {"profile": "text-quote", "exact": "version one"},
                "text": "change this",
                "idempotency_key": "review-idempotency-0002",
            },
        )

    assert response.status_code == 409
    assert response.json()["error"]["error"] == "stale_artifact_anchor"
    assert agent.calls == []


def test_native_comments_and_malformed_archives_are_bounded(tmp_path: Path) -> None:
    document = tmp_path / "comments.docx"
    _docx(document, "@clio tighten this paragraph")
    comments = extract_native_comments(document)

    assert len(comments) == 1
    assert comments[0].text == "@clio tighten this paragraph"
    assert comments[0].anchor.native_comment_id == "word:1"

    unsafe = tmp_path / "unsafe.docx"
    with zipfile.ZipFile(unsafe, "w") as archive:
        archive.writestr("../outside.xml", "<x/>")
    with pytest.raises(UnsafeDocumentArchiveError, match="path traversal"):
        extract_native_comments(unsafe)


def test_working_copy_save_mints_revision_and_stale_save_conflicts(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    document = root / "brief.docx"
    _docx(document)

    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as client:
        _workspace_id, session_id = _workspace_session(client, root)
        first = _pin(client, session_id, document.name)
        created = client.post(
            f"/v1/artifacts/{first['artifact_id']}/working-copies",
            json={
                "session_id": session_id,
                "provider": "native",
                "auto_checkpoint": False,
            },
        )
        assert created.status_code == 200, created.text
        working_copy = created.json()
        _docx(Path(working_copy["path"]), "@clio revise the title")
        updated = get_document_store(cast(FastAPI, client.app)).checkpoint(working_copy["id"])

        assert updated.head_version == 2
        assert updated.last_sha256 == hashlib.sha256(Path(updated.path).read_bytes()).hexdigest()
        reviews = client.get(f"/v1/artifacts/{updated.head_artifact_id}/reviews").json()["reviews"]

        document.write_bytes(b"external version")
        external = _pin(client, session_id, document.name)
        Path(updated.path).write_bytes(b"stale working copy save")
        conflict = get_document_store(cast(FastAPI, client.app)).checkpoint(updated.id)

    assert reviews[0]["native"] is True
    assert reviews[0]["text"].startswith("@clio")
    assert conflict.status == "conflict"
    assert conflict.conflict_head_artifact_id == external["artifact_id"]


def test_editor_tokens_are_short_lived_scoped_and_write_aware(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    token, expires = issue_access_token(
        app,
        working_copy_id="wc-one",
        provider="onlyoffice",
        writable=False,
    )

    payload = verify_access_token(
        app,
        token,
        working_copy_id="wc-one",
        provider="onlyoffice",
    )
    assert payload["exp"] == expires
    with pytest.raises(ValueError, match="incorrectly scoped"):
        verify_access_token(
            app,
            token,
            working_copy_id="wc-two",
            provider="onlyoffice",
        )
    with pytest.raises(ValueError, match="read-only"):
        verify_access_token(
            app,
            token,
            working_copy_id="wc-one",
            provider="onlyoffice",
            require_write=True,
        )


def test_editor_callback_download_requires_an_exact_origin() -> None:
    assert exact_http_origin(
        "https://editor.example.test/download/file",
        "https://editor.example.test",
    )
    assert exact_http_origin(
        "https://editor.example.test:443/download/file",
        "https://editor.example.test",
    )
    assert not exact_http_origin(
        "http://editor.example.test/download/file",
        "https://editor.example.test",
    )
    assert not exact_http_origin(
        "https://editor.example.test:444/download/file",
        "https://editor.example.test",
    )
    assert not exact_http_origin(
        "https://user:secret@editor.example.test/download/file",
        "https://editor.example.test",
    )


def test_collabora_wopi_locks_guard_editor_saves(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    document = root / "brief.odt"
    _odt(document)

    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as client:
        _workspace_id, session_id = _workspace_session(client, root)
        pinned = _pin(client, session_id, document.name)
        created = client.post(
            f"/v1/artifacts/{pinned['artifact_id']}/working-copies",
            json={
                "session_id": session_id,
                "provider": "collabora",
                "auto_checkpoint": False,
            },
        )
        assert created.status_code == 200, created.text
        working_copy = created.json()
        token, _expires = issue_access_token(
            cast(FastAPI, client.app),
            working_copy_id=working_copy["id"],
            provider="collabora",
            writable=True,
        )
        file_url = (
            "/v1/internal/document-editors/collabora/wopi/files/"
            f"{working_copy['id']}?access_token={token}"
        )
        contents_url = file_url.replace(f"?access_token={token}", f"/contents?access_token={token}")

        info = client.get(file_url)
        acquired = client.post(
            file_url,
            headers={"X-WOPI-Override": "LOCK", "X-WOPI-Lock": "lock-a"},
        )
        conflicting = client.post(
            file_url,
            headers={"X-WOPI-Override": "LOCK", "X-WOPI-Lock": "lock-b"},
        )
        blocked_save = client.post(
            contents_url,
            headers={"X-WOPI-Override": "PUT", "X-WOPI-Lock": "lock-b"},
            content=b"blocked",
        )
        replacement = tmp_path / "replacement.odt"
        _odt(replacement, "Edited text")
        saved = client.post(
            contents_url,
            headers={"X-WOPI-Override": "PUT", "X-WOPI-Lock": "lock-a"},
            content=replacement.read_bytes(),
        )
        released = client.post(
            file_url,
            headers={"X-WOPI-Override": "UNLOCK", "X-WOPI-Lock": "lock-a"},
        )

    assert info.status_code == 200
    assert info.json()["SupportsLocks"] is True
    assert acquired.status_code == 200
    assert conflicting.status_code == 409
    assert conflicting.headers["X-WOPI-Lock"] == "lock-a"
    assert blocked_save.status_code == 409
    assert saved.status_code == 200
    assert saved.headers["X-WOPI-ItemVersion"]
    assert released.status_code == 200


def test_document_autosave_steers_coalesce_but_reviews_do_not() -> None:
    inbox = LoopInbox()
    inbox.put_coalesced_user_message(
        InboxEvent(
            kind="user_message",
            task_id="",
            text="version 2",
            metadata={"coalesce_key": "document-save:one"},
        )
    )
    inbox.put_coalesced_user_message(
        InboxEvent(
            kind="user_message",
            task_id="",
            text="version 3",
            metadata={"coalesce_key": "document-save:one"},
        )
    )
    inbox.put(InboxEvent(kind="user_message", task_id="", text="explicit review"))

    events = inbox.drain()
    assert [event.text for event in events] == ["version 3", "explicit review"]


def test_markdown_rendition_prefers_local_typst_engine(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "brief.md"
    source.write_text("# Finding\n")
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    commands: list[list[str]] = []

    def find_executable(*names: str) -> str | None:
        if names == ("pandoc",):
            return "pandoc"
        if names == ("typst",):
            return "typst"
        return None

    def run(command: list[str], *, cwd: Path, timeout_seconds: float = 120.0) -> None:
        del cwd, timeout_seconds
        commands.append(command)
        Path(command[command.index("--output") + 1]).write_bytes(b"%PDF-1.7")

    monkeypatch.setattr(renditions, "_find_executable", find_executable)
    monkeypatch.setattr(renditions, "_run", run)

    rendered, converter = renditions._convert_to_pdf(source, output_dir)

    assert rendered.read_bytes() == b"%PDF-1.7"
    assert converter == "pandoc+typst"
    assert commands == [
        [
            "pandoc",
            str(source),
            "--output",
            str(rendered),
            "--pdf-engine",
            "typst",
            "--variable",
            f"mainfont={'Arial' if os.name == 'nt' else 'DejaVu Serif'}",
        ]
    ]


def test_latex_rendition_uses_only_cached_tectonic_resources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "paper.tex"
    source.write_text(r"\documentclass{article}\begin{document}Local\end{document}")
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    commands: list[list[str]] = []

    monkeypatch.setattr(
        renditions,
        "_find_executable",
        lambda *names: "tectonic" if names == ("tectonic",) else None,
    )

    def run(command: list[str], *, cwd: Path, timeout_seconds: float = 120.0) -> None:
        del cwd, timeout_seconds
        commands.append(command)
        (output_dir / "paper.pdf").write_bytes(b"%PDF-1.7")

    monkeypatch.setattr(renditions, "_run", run)

    rendered, converter = renditions._convert_to_pdf(source, output_dir)

    assert rendered.read_bytes() == b"%PDF-1.7"
    assert converter == "tectonic"
    assert commands == [
        [
            "tectonic",
            "--only-cached",
            "--keep-logs",
            "--outdir",
            str(output_dir),
            str(source),
        ]
    ]
