"""GET /v1/sessions/{sid}/context/files/content — serve context-file bytes.

The read counterpart of POST /attachments. Bytes ride back base64-in-JSON
(the CLIO Desktop transport bridge / SSH tunnel only forwards UTF-8 string
bodies, so raw binary cannot survive the round-trip), symmetric with the
upload route. Only files already in the session's context-file ledger are
served; the resolved path is re-confined to the workspace and a preview
size cap is enforced. The desktop capability-gates inline image/PDF/text
previews on ``x_clio_files_content``.
"""

from __future__ import annotations

import base64
import struct
import zlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


@pytest.fixture()
def setup(tmp_path: Path):
    app = build_app(sessions_path=tmp_path / "s.json")
    # Pin ws_default's root to tmp_path so uploads land under the policy boundary.
    app.state.workspaces.update("ws_default", root_path=str(tmp_path))
    return app, TestClient(app), tmp_path


def _sid(c: TestClient) -> str:
    return c.post("/v1/sessions", json={"title": "t"}).json()["id"]


def _upload(c: TestClient, sid: str, *, filename: str, content: bytes, mode: str = "read"):
    return c.post(
        f"/v1/sessions/{sid}/attachments",
        json={
            "file": base64.b64encode(content).decode("ascii"),
            "filename": filename,
            "mode": mode,
        },
    )


def _tiny_png() -> bytes:
    """A minimal but valid 1x1 RGB PNG (real IHDR/IDAT/IEND chunks)."""

    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(typ: bytes, data: bytes) -> bytes:
        body = typ + data
        return (
            struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\x00\x00")
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def test_text_attachment_round_trips_as_base64(setup) -> None:
    app, c, _tmp = setup
    sid = _sid(c)
    original = b"the quick brown fox\n"
    row = _upload(c, sid, filename="notes.txt", content=original).json()

    resp = c.get(f"/v1/sessions/{sid}/context/files/content", params={"path": row["path"]})

    assert resp.status_code == 200, resp.text
    file = resp.json()["file"]
    assert file["encoding"] == "base64"
    assert file["media_type"] == "text/plain; charset=utf-8"
    assert file["size"] == len(original)
    assert file["path"] == row["path"]
    assert base64.b64decode(file["data"]) == original


def test_png_attachment_media_type_and_exact_round_trip(setup) -> None:
    app, c, _tmp = setup
    sid = _sid(c)
    png = _tiny_png()
    row = _upload(c, sid, filename="pixel.png", content=png).json()

    resp = c.get(f"/v1/sessions/{sid}/context/files/content", params={"path": row["path"]})

    assert resp.status_code == 200, resp.text
    file = resp.json()["file"]
    assert file["media_type"] == "image/png"
    assert file["size"] == len(png)
    # Exact byte round-trip — no text mangling (unlike /files/read).
    assert base64.b64decode(file["data"]) == png


def test_png_sniffed_even_with_wrong_extension(setup) -> None:
    """Magic bytes win over a mislabeled extension."""

    app, c, _tmp = setup
    sid = _sid(c)
    png = _tiny_png()
    row = _upload(c, sid, filename="actually_a_png.txt", content=png).json()

    resp = c.get(f"/v1/sessions/{sid}/context/files/content", params={"path": row["path"]})

    assert resp.status_code == 200, resp.text
    assert resp.json()["file"]["media_type"] == "image/png"


def test_unregistered_path_is_404(setup) -> None:
    app, c, _tmp = setup
    sid = _sid(c)
    # A real file on disk, but never registered as a context file.
    stray = _tmp / "stray.txt"
    stray.write_text("not registered\n")

    resp = c.get(f"/v1/sessions/{sid}/context/files/content", params={"path": str(stray)})

    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["error"] == "not_found"
    assert "not registered" in body["error"]["message"]


def test_unknown_session_is_404(setup) -> None:
    _app, c, _tmp = setup
    resp = c.get("/v1/sessions/sess_nope/context/files/content", params={"path": "x.txt"})
    assert resp.status_code == 404
    assert resp.json()["error"]["error"] == "not_found"


def test_missing_path_param_is_422(setup) -> None:
    app, c, _tmp = setup
    sid = _sid(c)
    resp = c.get(f"/v1/sessions/{sid}/context/files/content", params={"path": "   "})
    assert resp.status_code == 422
    assert resp.json()["error"]["error"] == "bad_request"


@pytest.mark.parametrize(
    "traversal", ["../../etc/passwd", "..\\..\\windows\\system32\\drivers\\etc\\hosts"]
)
def test_traversal_paths_do_not_serve_files(setup, traversal: str) -> None:
    """A traversal path is never registered, so it 404s — bytes outside the
    workspace can never be reached through this route."""

    app, c, _tmp = setup
    sid = _sid(c)

    resp = c.get(f"/v1/sessions/{sid}/context/files/content", params={"path": traversal})

    # Not registered → structured 404 (the existing convention: only ledger
    # files are served, so traversal strings simply never match a row).
    assert resp.status_code in (403, 404)
    assert resp.json()["error"]["error"] in ("not_found", "path_outside_workspace")


def test_repointed_workspace_escape_is_403(setup) -> None:
    """If the workspace root is moved after registration so the registered
    file now sits outside it, the read-time confinement re-check rejects it."""

    app, c, tmp_path = setup
    sid = _sid(c)
    # Register a workspace-relative file under the original root.
    target = tmp_path / "inside.txt"
    target.write_text("inside\n")
    row = c.post(
        f"/v1/sessions/{sid}/context/files",
        json={"path": "inside.txt", "mode": "read"},
    ).json()
    assert row["path"] == "inside.txt"

    # Move the workspace root to a sibling dir; the resolved file is now
    # outside the (new) root boundary.
    new_root = tmp_path / "elsewhere"
    new_root.mkdir()
    app.state.workspaces.update("ws_default", root_path=str(new_root))

    resp = c.get(f"/v1/sessions/{sid}/context/files/content", params={"path": "inside.txt"})

    assert resp.status_code == 403
    assert resp.json()["error"]["error"] == "path_outside_workspace"


def test_file_gone_from_disk_is_404(setup) -> None:
    app, c, tmp_path = setup
    sid = _sid(c)
    row = _upload(c, sid, filename="ephemeral.txt", content=b"bye").json()
    # Delete the bytes out from under the ledger entry.
    Path(row["resolved_path"]).unlink()

    resp = c.get(f"/v1/sessions/{sid}/context/files/content", params={"path": row["path"]})

    assert resp.status_code == 404
    assert resp.json()["error"]["error"] == "not_found"


def test_oversize_file_is_413(setup, monkeypatch) -> None:
    app, c, _tmp = setup
    sid = _sid(c)
    # Shrink the preview cap so the test stays fast (no 10 MiB write).
    import clio_agent.gact.app as gact_app  # noqa: PLC0415

    # The cap is a closure local in build_app; instead drive it via the file
    # policy cap, which the route min()s against. Set a tiny policy cap.
    monkeypatch.setenv("CLIO_MAX_FILE_SIZE_BYTES", "8")
    row = _upload(c, sid, filename="big.txt", content=b"0123456789").json()  # 10 bytes > 8

    resp = c.get(f"/v1/sessions/{sid}/context/files/content", params={"path": row["path"]})

    assert resp.status_code == 413, resp.text
    body = resp.json()
    assert body["error"]["error"] == "payload_too_large"
    assert body["error"]["details"]["max_bytes"] == 8
    # Reference gact_app so the import isn't flagged unused by linters.
    assert hasattr(gact_app, "build_app")


def test_workspace_context_file_by_path_can_be_fetched(setup) -> None:
    """A context file registered by path (not uploaded) is fetchable too."""

    app, c, tmp_path = setup
    sid = _sid(c)
    target = tmp_path / "src" / "module.py"
    target.parent.mkdir()
    target.write_text("def f():\n    return 1\n")
    row = c.post(
        f"/v1/sessions/{sid}/context/files",
        json={"path": "@src/module.py", "mode": "read"},
    ).json()
    assert row["path"] == "src/module.py"

    resp = c.get(f"/v1/sessions/{sid}/context/files/content", params={"path": "src/module.py"})

    assert resp.status_code == 200, resp.text
    file = resp.json()["file"]
    assert file["media_type"] == "text/x-python; charset=utf-8"
    assert base64.b64decode(file["data"]) == target.read_bytes()
    assert file["display_path"] == "src/module.py"


def test_lookup_by_display_and_resolved_path(setup) -> None:
    """The same file is fetchable by display_path and resolved_path keys."""

    app, c, tmp_path = setup
    sid = _sid(c)
    target = tmp_path / "doc.md"
    target.write_text("# title\n")
    row = c.post(
        f"/v1/sessions/{sid}/context/files",
        json={"path": "doc.md", "mode": "read"},
    ).json()

    for key in (row["path"], row["display_path"], row["resolved_path"]):
        resp = c.get(f"/v1/sessions/{sid}/context/files/content", params={"path": key})
        assert resp.status_code == 200, f"{key} -> {resp.text}"
        assert resp.json()["file"]["media_type"] == "text/markdown; charset=utf-8"


def test_workspace_row_absolute_path_outside_root_is_403(setup) -> None:
    """Review fix: a row WITH a workspace_id must be confined to that
    workspace root even when the registered path is ABSOLUTE.

    An absolute path that resolves outside ws_default's root is registered
    (the registration route stamps every row with the session's workspace_id,
    here ws_default). At read time the resolved path is outside the root, so
    the confinement re-check must reject it — an absolute display path is no
    longer a free pass past the workspace boundary.
    """

    app, c, tmp_path = setup
    sid = _sid(c)
    # A real file that lives OUTSIDE ws_default's root (tmp_path). Its parent
    # is, by construction, not under tmp_path.
    outside = tmp_path.parent / f"outside_{tmp_path.name}.txt"
    outside.write_text("secret outside the workspace\n")
    try:
        # Register by ABSOLUTE path. The route resolves it as-is and stamps it
        # with workspace_id=ws_default (the session's workspace).
        row = c.post(
            f"/v1/sessions/{sid}/context/files",
            json={"path": str(outside), "mode": "read"},
        ).json()
        assert row["workspace_id"] == "ws_default"
        assert Path(row["display_path"]).is_absolute()

        resp = c.get(
            f"/v1/sessions/{sid}/context/files/content", params={"path": row["path"]}
        )
        assert resp.status_code == 403, resp.text
        body = resp.json()
        assert body["error"]["error"] == "path_outside_workspace"
        assert body["error"]["details"]["workspace_id"] == "ws_default"
    finally:
        outside.unlink(missing_ok=True)


def test_workspace_row_absolute_path_inside_root_is_200(setup) -> None:
    """Review fix companion: a row WITH a workspace_id whose absolute path
    resolves INSIDE the workspace root is still served (200)."""

    app, c, tmp_path = setup
    sid = _sid(c)
    inside = tmp_path / "report.txt"
    inside.write_text("inside the workspace\n")
    # Register by ABSOLUTE path pointing under ws_default's root.
    row = c.post(
        f"/v1/sessions/{sid}/context/files",
        json={"path": str(inside), "mode": "read"},
    ).json()
    assert row["workspace_id"] == "ws_default"
    assert Path(row["display_path"]).is_absolute()

    resp = c.get(f"/v1/sessions/{sid}/context/files/content", params={"path": row["path"]})
    assert resp.status_code == 200, resp.text
    assert base64.b64decode(resp.json()["file"]["data"]) == inside.read_bytes()


def test_no_workspace_row_absolute_path_served_as_is_200(setup) -> None:
    """Review fix companion: a row with NO workspace_id has no boundary to
    enforce, so an absolute registered path is served as-is (200), even when
    it sits outside any workspace root.

    The registration route always stamps a workspace_id, so this legacy /
    boundary-less shape is injected directly into the context-file ledger —
    exactly the row the endpoint must honor without confinement.
    """

    app, c, tmp_path = setup
    sid = _sid(c)
    # A real file outside any workspace root, registered with NO workspace_id.
    outside = tmp_path.parent / f"boundaryless_{tmp_path.name}.txt"
    outside.write_text("no workspace boundary\n")
    resolved = str(Path(outside).resolve())
    try:
        bucket = app.state.context_files.setdefault(sid, {})
        bucket[resolved] = {
            "path": resolved,
            "display_path": resolved,
            "resolved_path": resolved,
            "workspace_id": "",  # no boundary
            "source": "api",
            "mode": "read",
        }

        resp = c.get(
            f"/v1/sessions/{sid}/context/files/content", params={"path": resolved}
        )
        assert resp.status_code == 200, resp.text
        assert base64.b64decode(resp.json()["file"]["data"]) == outside.read_bytes()
    finally:
        outside.unlink(missing_ok=True)


def test_capabilities_advertise_files_content(setup) -> None:
    _app, c, _tmp = setup
    caps = c.get("/v1/capabilities").json()["capabilities"]
    assert caps["x_clio_files_content"] is True
