"""Document artifact protocol, review, working-copy, and editor tests."""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import zipfile
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.task_types import ContextBoundary, MailItem
from clio_agent.gact.app import build_app
from clio_agent.gact.documents import DocumentWorkingCopy, renditions
from clio_agent.gact.documents.native_comments import (
    UnsafeDocumentArchiveError,
    extract_native_comments,
)
from clio_agent.gact.documents.profiles import document_format
from clio_agent.gact.documents.store import get_document_store
from clio_agent.gact.native_work import now
from clio_agent.gact.session_input import accept_session_input
from clio_agent.gact.task_access import TaskCaller
from tests.test_arc.conftest import core_arc as core_arc
from tests.test_arc.test_conversation_owner_contracts import create_environment
from tests.test_gact.test_post_messages import FakeClioAgent

pytestmark = pytest.mark.usefixtures("host_agent_executor")

# A loaded CI runner can take seconds to boot the app and dispatch a turn; the per-test
# hang guard (180 s) still bounds a dispatch that never comes.
_DISPATCH_WAIT_S = 120


class _SignallingAgent(FakeClioAgent):
    """FakeClioAgent that signals each dispatch, so a test awaits it, not a wall clock."""

    def __init__(self) -> None:
        super().__init__()
        self.dispatched = threading.Event()

    def forward(self, question: str, session_id: str) -> object:
        try:
            return super().forward(question, session_id)
        finally:
            self.dispatched.set()


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
    assert "embedded_editors" not in capability
    assert document_format("report.docx").profile == "ooxml-word"
    assert document_format("model.odp").rendition_formats == ("pdf",)
    assert document_format("paper.tex").anchors == (
        "text-quote",
        "pdf-quad",
        "region",
        "source-map",
    )


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
    assert manifest.json()["anchors"] == ["text-quote", "region", "dom"]
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
    agent = _SignallingAgent()

    with TestClient(build_app(sessions_path=tmp_path / "sessions.json", agent=agent)) as client:
        _workspace_id, session_id = _workspace_session(client, root)
        pinned = _pin(client, session_id, markdown.name)
        payload = {
            "artifact_id": pinned["artifact_id"],
            "expected_version": pinned["version"],
            "expected_sha256": pinned["sha256"],
            "marks": [
                {
                    "anchor": {
                        "profile": "text-quote",
                        "exact": "Initial text.",
                        "prefix": "# Finding\n",
                    },
                    "comment": "Make this conclusion more precise.",
                }
            ],
            "idempotency_key": "review-idempotency-0001",
        }
        first = client.post(f"/v1/sessions/{session_id}/comment-sessions", json=payload)
        second = client.post(f"/v1/sessions/{session_id}/comment-sessions", json=payload)
        assert agent.dispatched.wait(_DISPATCH_WAIT_S), "the review was never dispatched"
        messages = client.get(f"/v1/sessions/{session_id}/messages").json()["messages"]

    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    assert first.json()["threads"][0]["id"] == second.json()["threads"][0]["id"]
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
            f"/v1/sessions/{session_id}/comment-sessions",
            json={
                "artifact_id": first["artifact_id"],
                "expected_version": first["version"],
                "expected_sha256": first["sha256"],
                "marks": [
                    {
                        "anchor": {"profile": "text-quote", "exact": "version one"},
                        "comment": "change this",
                    }
                ],
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


def test_working_copy_reloads_from_long_canonical_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep actual manifest IO working beyond Windows MAX_PATH after a restart."""
    from clio_agent import paths
    from clio_agent.gact.documents.store import DocumentStore

    root = tmp_path / "workspace"
    root.mkdir()
    document = root / "brief.docx"
    _docx(document)
    state_root = tmp_path.joinpath(*(["canonical-document-state-" + "x" * 32] * 5))
    assert len(str(state_root)) > 260

    def workspace_state(cwd: str | Path | None = None) -> Path:
        return state_root

    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as client:
        _workspace_id, session_id = _workspace_session(client, root)
        first = _pin(client, session_id, document.name)
        monkeypatch.setattr(paths, "workspace_agent_dir", workspace_state)
        created = client.post(
            f"/v1/artifacts/{first['artifact_id']}/working-copies",
            json={"session_id": session_id, "auto_checkpoint": False},
        )
        assert created.status_code == 200, created.text
        working_copy = created.json()
        restored = DocumentStore(cast(FastAPI, client.app)).get_working_copy(working_copy["id"])
        assert restored is not None
        assert Path(restored.path).read_bytes() == document.read_bytes()
        assert Path(restored.path).is_relative_to(root / "artifacts" / "document-working-copies")


def test_working_copy_save_mints_revision_and_stale_save_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    document = root / "brief.docx"
    _docx(document)
    # A Word comment saved in the file resolves against the file: dispatching it renders no
    # PDF (which would provision the Office runtime, a download, on a machine without it).
    from clio_agent.gact.documents import renditions

    def no_render(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("a native comment must not render the document")

    monkeypatch.setattr(renditions, "render_pdf", no_render)
    monkeypatch.setattr(renditions, "find_pdf_rendition", no_render)

    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as client:
        workspace_id, session_id = _workspace_session(client, root)
        first = _pin(client, session_id, document.name)
        assert first["media_type"] == document_format(document.name).mime_type
        listing = client.get(f"/v1/sessions/{session_id}/artifacts").json()
        assert listing["artifacts"][0]["versions"][0]["media_type"] == first["media_type"]
        created = client.post(
            f"/v1/artifacts/{first['artifact_id']}/working-copies",
            json={
                "session_id": session_id,
                "auto_checkpoint": False,
            },
        )
        assert created.status_code == 200, created.text
        working_copy = created.json()
        editable_path = Path(working_copy["path"])
        assert editable_path.is_relative_to(root / "artifacts" / "document-working-copies")
        listing = client.get(
            f"/v1/workspaces/{workspace_id}/files?include_hidden=true&exclude_service_storage=true"
        ).json()
        assert str(editable_path.relative_to(root)) in {row["path"] for row in listing["entries"]}
        from clio_agent import paths
        from clio_agent.gact.documents.store import DocumentStore

        manifest = (
            paths.workspace_agent_dir(root)
            / "documents"
            / "working-copies"
            / working_copy["id"]
            / "manifest.json"
        )
        from clio_agent.platform_paths import win_extended_path

        assert Path(win_extended_path(manifest)).is_file()
        assert not (editable_path.parent / "manifest.json").exists()
        restored = DocumentStore(cast(FastAPI, client.app)).get_working_copy(working_copy["id"])
        assert restored is not None
        assert restored.path == working_copy["path"]
        _docx(Path(working_copy["path"]), "@clio revise the title")
        updated = get_document_store(cast(FastAPI, client.app)).checkpoint(working_copy["id"])

        assert updated.head_version == 2
        assert updated.last_sha256 == hashlib.sha256(Path(updated.path).read_bytes()).hexdigest()
        reviews = client.get(f"/v1/artifacts/{updated.head_artifact_id}/threads").json()["threads"]

        document.write_bytes(b"external version")
        external = _pin(client, session_id, document.name)
        Path(updated.path).write_bytes(b"stale working copy save")
        conflict = get_document_store(cast(FastAPI, client.app)).checkpoint(updated.id)

    assert reviews[0]["native"] is True
    assert reviews[0]["text"].startswith("@clio")
    assert "must not render" not in (reviews[0].get("error") or "")
    assert conflict.status == "conflict"
    assert conflict.conflict_head_artifact_id == external["artifact_id"]


def test_embedded_editor_routes_are_gone_and_old_working_copies_still_load(
    tmp_path: Path,
) -> None:
    """The ONLYOFFICE and Collabora routes are deleted; a working copy stored with the old
    ``provider`` field still validates, since the model ignores keys it no longer has."""

    with TestClient(build_app(sessions_path=tmp_path / "sessions.json")) as client:
        assert client.get("/v1/document-editors/health").status_code == 404
        assert (
            client.post(
                "/v1/document-working-copies/wc_missing/editor-sessions",
                json={"provider": "onlyoffice"},
            ).status_code
            == 404
        )
    stored = {
        "id": "docwc_old",
        "session_id": "sess_old",
        "workspace_id": "ws_old",
        "artifact_name": "brief.docx",
        "base_artifact_id": "artifact_a",
        "head_artifact_id": "artifact_a",
        "base_version": 1,
        "head_version": 1,
        "base_sha256": "0" * 64,
        "last_sha256": "0" * 64,
        "path": str(tmp_path / "brief.docx"),
        "provider": "onlyoffice",
        "writable": True,
        "auto_checkpoint": True,
        "created_at": "2026-11-05T00:00:00+00:00",
        "updated_at": "2026-11-05T00:00:00+00:00",
    }
    restored = DocumentWorkingCopy.model_validate(stored)
    assert restored.id == "docwc_old"
    assert not hasattr(restored, "provider")


@pytest.mark.asyncio
@pytest.mark.parametrize("withdrawal_fault", [False, True])
async def test_document_autosave_steers_coalesce_but_reviews_do_not(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, withdrawal_fault: bool
) -> None:
    """Only uncommitted autosaves coalesce; complete history and explicit reviews remain."""
    app, owner, root = await create_environment(arc, tmp_path)
    sid = root.owner.session_id
    history, caller = owner.history, TaskCaller(sid, "human")

    def mail_text(item: MailItem) -> str:
        body = history.read_content(item.content)
        assert isinstance(body, dict) and isinstance(body.get("text"), str)
        text = body["text"]
        assert isinstance(text, str)
        return text

    def exercise() -> None:
        metadata = {"coalesce_key": "document-save:one"}
        first = accept_session_input(
            app, sid, "version 2", metadata=metadata, steer_message_id="save-2"
        )
        staged = owner.mail.stage(caller)
        assert tuple(item.id for item in staged.items) == (first,)
        if withdrawal_fault:
            original = history.withdraw_mail

            def fault(_sid: str, _identity: str) -> None:
                raise OSError("Withdrawal recording unavailable after new notice acceptance")

            monkeypatch.setattr(history, "withdraw_mail", fault)
            with pytest.raises(OSError, match="Withdrawal recording unavailable"):
                accept_session_input(
                    app, sid, "version 3", metadata=metadata, steer_message_id="save-3"
                )
            monkeypatch.setattr(history, "withdraw_mail", original)
            history.restore()
            owner.mail.stage(caller)
        latest = accept_session_input(
            app, sid, "version 3", metadata=metadata, steer_message_id="save-3"
        )
        review = MailItem(
            id="explicit-review",
            recipient_session=sid,
            kind="human",
            sender_human="human",
            created_at=now(),
            content=history.write_content(sid, {"text": "explicit review"}),
        )
        history.record_mail(review)
        assert [mail_text(item) for item in history.pending_mail(sid)] == [
            "version 3",
            "explicit review",
        ]
        for _ in range(2):
            history.restore()
            assert tuple(item.id for item in history.pending_mail(sid)) == (latest, review.id)
        assert [
            mail_text(history.require_mail(sid, identity))
            for identity in (first, latest, review.id)
        ] == ["version 2", "version 3", "explicit review"]
        with pytest.raises(ValueError, match="Staged input changed"):
            owner.mail.commit(
                caller,
                staged,
                assignment_id=root.handle,
                iteration_id="stale",
                boundary=ContextBoundary(session_id=sid, scope="autosave", logical_time=0),
            )
        assert not history.acknowledged_notifications(sid)
        current = owner.mail.stage(caller)
        owner.mail.commit(
            caller,
            current,
            assignment_id=root.handle,
            iteration_id="newest",
            boundary=ContextBoundary(session_id=sid, scope="autosave", logical_time=0),
        )
        assert history.acknowledged_notifications(sid) == frozenset({latest, review.id})
        assert history.mail_withdrawn(sid, first)

    try:
        await asyncio.to_thread(exercise)
    finally:
        await owner.shutdown()
        await owner.recovery.drain()


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

    def run(command: list[str], *, cwd: Path, office: bool = False) -> None:
        del cwd, office
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


def test_latex_never_falls_through_to_pandoc(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A .tex source renders only through the LaTeX project build, never pandoc (L rule 1)."""
    source = tmp_path / "paper.tex"
    source.write_text(r"\documentclass{article}\begin{document}Local\end{document}")
    monkeypatch.setattr(renditions, "_find_executable", lambda *names: "/usr/bin/pandoc")
    with pytest.raises(renditions.RenditionError, match="project build"):
        renditions._convert_to_pdf(source, tmp_path)
