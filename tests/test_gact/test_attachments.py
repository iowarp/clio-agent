"""POST /v1/sessions/{sid}/attachments — upload bytes into the workspace.

Bytes arrive base64-in-JSON (the CLIO Desktop transport bridge only
forwards UTF-8 string bodies, so multipart cannot survive the shipped
desktop / an SSH tunnel). The upload is written under
``{workspace_root}/.clio/attachments/{sid}/`` and registered as a
context file, so the existing agent read path consumes it next turn.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


class _RecordingAgent:
    """Captures every question forwarded to it (mirror test_context_injection)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def forward(self, question: str, session_id: str = "default"):
        self.calls.append((question, session_id))
        return type(
            "Pred",
            (),
            {"answer": "ok", "selected_expert": "", "routing_rationale": ""},
        )()


@pytest.fixture()
def setup(tmp_path: Path):
    agent = _RecordingAgent()
    app = build_app(sessions_path=tmp_path / "s.json", agent=agent)
    # Pin ws_default's root to tmp_path so uploads land under the policy boundary.
    app.state.workspaces.update("ws_default", root_path=str(tmp_path))
    return app, TestClient(app), agent, tmp_path


def _upload(c: TestClient, sid: str, *, filename: str, content: bytes, mode: str = "read"):
    return c.post(
        f"/v1/sessions/{sid}/attachments",
        json={
            "file": base64.b64encode(content).decode("ascii"),
            "filename": filename,
            "mode": mode,
        },
    )


def test_upload_writes_into_workspace_and_registers(setup) -> None:
    app, c, _agent, tmp_path = setup
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]

    resp = _upload(c, sid, filename="notes.md", content=b"hello body")

    assert resp.status_code == 200, resp.text
    row = resp.json()
    assert row["mode"] == "read"
    assert row["path"].replace("\\", "/").endswith(f".clio/attachments/{sid}/notes.md")
    # Bytes really landed on disk inside the workspace.
    on_disk = tmp_path / ".clio" / "attachments" / sid / "notes.md"
    assert on_disk.is_file()
    assert on_disk.read_bytes() == b"hello body"
    # And it's in the context-file ledger.
    listed = c.get(f"/v1/sessions/{sid}/context/files").json()["files"]
    assert any(f["path"] == row["path"] for f in listed)


def test_upload_preserves_media_type_metadata(setup) -> None:
    app, c, _agent, _tmp = setup
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]

    resp = c.post(
        f"/v1/sessions/{sid}/attachments",
        json={
            "file": base64.b64encode(b"\x89PNG\r\n").decode("ascii"),
            "filename": "cells.png",
            "media_type": "image/png",
        },
    )

    assert resp.status_code == 200, resp.text
    row = resp.json()
    assert row["media_type"] == "image/png"
    assert row["mime_type"] == "image/png"
    listed = c.get(f"/v1/sessions/{sid}/context/files").json()["files"]
    listed_row = next(item for item in listed if item["path"] == row["path"])
    assert listed_row["media_type"] == "image/png"


def test_upload_consumed_by_agent_next_turn(setup) -> None:
    from .conftest import complete_turn

    app, c, agent, _tmp = setup
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]

    _upload(c, sid, filename="insight.txt", content=b"important upload insight")
    complete_turn(c, sid, "summarise")

    seen, _ = agent.calls[-1]
    # The end-to-end proof: uploaded bytes reach the agent's prompt via the
    # existing context-file read path, with no agent-layer change.
    assert "important upload insight" in seen
    assert "summarise" in seen


def test_upload_path_traversal_is_confined_to_attachments_dir(setup) -> None:
    app, c, _agent, tmp_path = setup
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
    base = tmp_path / ".clio" / "attachments" / sid

    # Posix- and Windows-style traversal attempts.
    for bad in ("../../evil.txt", "..\\..\\evil2.txt"):
        resp = _upload(c, sid, filename=bad, content=b"pwned")
        assert resp.status_code == 200, resp.text
        landed = resp.json()["path"]
        # Reduced to a bare basename inside the attachments dir.
        assert landed.replace("\\", "/").startswith(f".clio/attachments/{sid}/")
        assert "evil" in Path(landed).name

    # NOTHING was written outside the attachments dir.
    assert not (tmp_path / "evil.txt").exists()
    assert not (tmp_path.parent / "evil.txt").exists()
    assert not (tmp_path / "evil2.txt").exists()
    assert (base / "evil.txt").is_file()
    assert (base / "evil2.txt").is_file()


def test_upload_rejects_dotdot_only_and_empty_names(setup) -> None:
    app, c, _agent, _tmp = setup
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
    for bad in ("..", "", "   "):
        resp = _upload(c, sid, filename=bad, content=b"x")
        assert resp.status_code == 422, f"{bad!r} -> {resp.status_code}"


def test_upload_collision_does_not_overwrite(setup) -> None:
    app, c, _agent, tmp_path = setup
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]

    first = _upload(c, sid, filename="notes.md", content=b"one").json()
    second = _upload(c, sid, filename="notes.md", content=b"two").json()

    assert first["path"] != second["path"]
    base = tmp_path / ".clio" / "attachments" / sid
    assert (base / "notes.md").read_bytes() == b"one"
    assert (base / "notes (2).md").read_bytes() == b"two"
    listed = c.get(f"/v1/sessions/{sid}/context/files").json()["files"]
    assert len(listed) == 2


def test_upload_oversize_rejected(setup) -> None:
    app, c, _agent, _tmp = setup
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]

    resp = _upload(c, sid, filename="big.bin", content=b"\x00" * (25 * 1024 * 1024 + 1))

    assert resp.status_code == 413
    assert resp.json()["error"]["error"] == "payload_too_large"


def test_upload_emits_context_file_added(setup) -> None:
    app, c, _agent, _tmp = setup
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]

    _upload(c, sid, filename="notes.md", content=b"hello")

    history = app.state.bus._history.get(sid, [])
    added = [e for e in history if e.type == "context.file.added"]
    assert len(added) == 1
    assert added[0].payload["file"]["mode"] == "read"


def test_upload_bad_base64_rejected(setup) -> None:
    app, c, _agent, _tmp = setup
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]

    resp = c.post(
        f"/v1/sessions/{sid}/attachments",
        json={"file": "!!!not base64!!!", "filename": "x.txt"},
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["error"] == "bad_request"


def test_capabilities_advertise_attachments_upload(setup) -> None:
    app, c, _agent, _tmp = setup
    caps = c.get("/v1/capabilities").json()["capabilities"]
    assert caps["attachments_upload"] is True


# --- hardening (#527 review response) ---------------------------------------


def test_concurrent_same_name_uploads_both_land_without_corruption(
    setup, monkeypatch
) -> None:
    """Two uploads racing for the SAME destination filename must each land
    intact — the unique per-upload temp file prevents a shared ".tmp" name
    from clobbering the other writer's bytes.

    We force the two attachment byte-writes to genuinely overlap on disk: a
    barrier inside os.replace blocks each request until BOTH temp files have
    been created and written, then releases them together. With the old
    deterministic temp name ("dest.name + .tmp") the two writers would have
    shared a single temp file and one would have clobbered/raced the other;
    with a unique per-upload temp file they coexist and both survive.

    The shared context-files JSON ledger flush (_flush_context_files) uses
    its own deterministic temp name and is NOT part of this review's concern;
    we serialize it under a lock here so its pre-existing single-writer
    assumption doesn't mask the attachment-write property under test.
    """
    import threading

    import clio_agent.gact.app as gact_app

    app, c, _agent, tmp_path = setup
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
    base = tmp_path / ".clio" / "attachments" / sid

    # Serialize the unrelated shared-ledger flush (single-writer by design).
    flush_lock = threading.Lock()
    real_flush = gact_app._flush_context_files

    def _locked_flush(app_arg):
        with flush_lock:
            return real_flush(app_arg)

    monkeypatch.setattr(gact_app, "_flush_context_files", _locked_flush)

    # Barrier across the two attachment renames: hold each until BOTH temp
    # files exist, proving they coexisted (i.e. did not share one ".tmp").
    rename_barrier = threading.Barrier(2, timeout=30)
    real_replace = gact_app.os.replace
    temp_names_seen: list[str] = []
    seen_lock = threading.Lock()

    def _barriered_replace(src, dst, *a, **k):
        s = str(src)
        # Only gate the attachment temp renames (".tmp" inside our base dir);
        # leave the ledger's own replace alone.
        if s.endswith(".tmp") and str(base) in s:
            with seen_lock:
                temp_names_seen.append(s)
            rename_barrier.wait()
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(gact_app.os, "replace", _barriered_replace)

    payload_a = b"A" * (512 * 1024)
    payload_b = b"B" * (512 * 1024)
    results: dict[str, object] = {}

    def _go(key: str, content: bytes) -> None:
        results[key] = _upload(c, sid, filename="race.bin", content=content)

    t_a = threading.Thread(target=_go, args=("a", payload_a))
    t_b = threading.Thread(target=_go, args=("b", payload_b))
    t_a.start()
    t_b.start()
    t_a.join(timeout=30)
    t_b.join(timeout=30)

    resp_a = results["a"]
    resp_b = results["b"]
    assert resp_a.status_code == 200, resp_a.text  # type: ignore[union-attr]
    assert resp_b.status_code == 200, resp_b.text  # type: ignore[union-attr]

    # THE proof: the two concurrent writers held DISTINCT temp files at the
    # same instant. With the old deterministic "dest.name + .tmp" both would
    # have been the SAME path and one writer's bytes would have raced/clobbered
    # the other's mid-write; the unique per-upload temp file makes them
    # disjoint so neither can corrupt the other.
    assert len(temp_names_seen) == 2
    assert temp_names_seen[0] != temp_names_seen[1], temp_names_seen

    # Two distinct destinations were claimed atomically (O_CREAT|O_EXCL), so
    # neither upload overwrote the other: "race.bin" and "race (2).bin".
    path_a = resp_a.json()["path"]  # type: ignore[union-attr]
    path_b = resp_b.json()["path"]  # type: ignore[union-attr]
    assert path_a != path_b

    # No temp artifact leaked once the requests completed.
    leftovers = [p.name for p in base.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == [], leftovers

    landed = {p.name: p.read_bytes() for p in base.iterdir() if p.is_file()}
    assert set(landed) == {"race.bin", "race (2).bin"}, landed
    # Both payloads survived intact and un-interleaved — homogeneous bytes, so
    # any spliced write would show up as a mixed blob (e.g. b"AAA...BBB").
    # Each landed file is exactly one payload, and the two together cover both.
    assert {bytes(b) for b in landed.values()} == {payload_a, payload_b}


def test_oversize_encoded_rejected_before_decode(setup, monkeypatch) -> None:
    """An encoded payload whose minimum decoded size already exceeds the cap
    is rejected 413 WITHOUT ever calling base64.b64decode (no ~25 MiB+
    allocation for a request we know we will refuse)."""
    import base64 as _b64

    app, c, _agent, _tmp = setup
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]

    called = {"decode": False}
    real_decode = _b64.b64decode

    def _tripwire(*args, **kwargs):  # pragma: no cover - must not run
        called["decode"] = True
        return real_decode(*args, **kwargs)

    monkeypatch.setattr(_b64, "b64decode", _tripwire)

    # An all-'A' base64 string of this length decodes to well over 25 MiB.
    # We never decode it, so building the string is cheap relative to the
    # decoded bytes we are refusing to allocate.
    over_cap_chars = ((25 * 1024 * 1024) // 3 + 16) * 4
    huge_encoded = "A" * over_cap_chars

    resp = c.post(
        f"/v1/sessions/{sid}/attachments",
        json={"file": huge_encoded, "filename": "huge.bin"},
    )

    assert resp.status_code == 413, resp.text
    assert resp.json()["error"]["error"] == "payload_too_large"
    assert called["decode"] is False, "b64decode must NOT run for an over-cap payload"
    # And nothing was written to disk.
    base = _tmp / ".clio" / "attachments" / sid
    assert not base.exists() or not any(base.iterdir())


def test_traversal_attempt_writes_no_file_outside_dir(setup) -> None:
    """A posix/windows traversal name is reduced to a bare basename, so the
    only file that exists afterwards is inside the attachments dir — nothing
    is written to the parent/escape target. (Confinement is validated before
    the write, so an escape can never produce a stray file.)"""
    app, c, _agent, tmp_path = setup
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]

    _upload(c, sid, filename="../../escape.bin", content=b"pwned")

    # The escape targets never materialized anywhere up the tree.
    assert not (tmp_path / "escape.bin").exists()
    assert not (tmp_path.parent / "escape.bin").exists()
    assert not (tmp_path.parent.parent / "escape.bin").exists()
    # The basename landed (only) inside the confined attachments dir.
    base = tmp_path / ".clio" / "attachments" / sid
    assert (base / "escape.bin").read_bytes() == b"pwned"


def test_confinement_rejected_before_any_file_written(setup, monkeypatch) -> None:
    """If the post-collision destination escapes `base`, the route returns
    403 and NEVER opens a temp file — the confinement guard runs before any
    byte (even a temp byte) is written. We simulate an escape by making the
    dest's relative_to(base) raise, then assert tempfile.mkstemp was never
    called and the attachments dir holds no files."""
    import tempfile as _tempfile

    app, c, _agent, tmp_path = setup
    sid = c.post("/v1/sessions", json={"title": "t"}).json()["id"]
    base = tmp_path / ".clio" / "attachments" / sid

    opened = {"mkstemp": False}
    real_mkstemp = _tempfile.mkstemp

    def _tripwire(*args, **kwargs):  # pragma: no cover - must not run
        opened["mkstemp"] = True
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr(_tempfile, "mkstemp", _tripwire)

    # Make the confinement check (dest.resolve().relative_to(base)) trip by
    # raising ValueError when relative_to is called against the attachments
    # base path. The route catches ValueError -> 403 path_outside_workspace.
    real_relative_to = Path.relative_to
    base_resolved = base.resolve()

    def _maybe_escape(self, *other):
        if other and Path(other[0]) == base_resolved:
            raise ValueError("simulated escape")
        return real_relative_to(self, *other)

    monkeypatch.setattr(Path, "relative_to", _maybe_escape)

    resp = _upload(c, sid, filename="escape.bin", content=b"pwned")

    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["error"] == "path_outside_workspace"
    assert opened["mkstemp"] is False, "no temp file may be opened on a rejected path"
    assert not base.exists() or not any(p.is_file() for p in base.iterdir())
