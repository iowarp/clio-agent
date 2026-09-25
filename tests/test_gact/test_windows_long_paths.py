"""Windows long-path (MAX_PATH) regression tests for W1's hardened write sites.

Windows limits an ordinary path to 260 characters unless the system-wide
``LongPathsEnabled`` registry policy is on. CLIO cannot assume that policy is
set (observed live: Windows 11, ``LongPathsEnabled=0``, a real workspace root
plus the resource-upload staging name landed at ~270 characters and every
write under it raised ``FileNotFoundError`` even though every parent
directory existed -- see :mod:`clio_agent.platform_paths`).

These tests build a REAL path over that limit under ``tmp_path`` and assert
the write actually succeeds end to end: resource materialization
(:mod:`clio_agent.gact.resource_materialization`), the documents store
(:mod:`clio_agent.gact.documents.store`), and the CAS backbone both depend on
(:mod:`clio_agent.gact.artifacts.cas`). CI runs Linux, so the whole module is
skipped there with an explicit reason -- the platform-independent string logic
of the shared helper is covered everywhere by ``test_core/test_platform_paths.py``.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from clio_agent.gact import resource_materialization
from clio_agent.gact.artifacts import cas
from clio_agent.gact.artifacts.records import (
    ArtifactRecord,
    ArtifactVersion,
    Custody,
    IdentityEvidence,
)
from clio_agent.gact.documents import store as document_store_module
from clio_agent.gact.resource_custody import ResourceStore
from clio_agent.platform_paths import win_extended_path

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("win"),
    reason="MAX_PATH (260-char) path length is a Windows-specific limit; CI runs Linux.",
)


def _padded_root(tmp_path: Path, *, target_len: int = 240) -> Path:
    """A real, existing directory under ``tmp_path`` padded past ``target_len`` chars.

    Building the fixture itself hits the very MAX_PATH limit under test --
    plain ``Path.mkdir`` fails once the composed path crosses ~259 characters
    (confirmed live on this box), so the padded tree is created through
    :func:`win_extended_path` too. That's fair: it's test scaffolding, not the
    production code path being exercised.
    """

    root = tmp_path / "ws"
    segment = "review-stack-segment"
    while len(str(root)) < target_len:
        root = root / segment
    os.makedirs(win_extended_path(root), exist_ok=True)
    return root


# --------------------------------------------------------------------------- #
# resource_materialization.py — the exact observed live bug.
# --------------------------------------------------------------------------- #


def _ingest_ready_resource(store: ResourceStore, *, workspace_id: str, name: str, content: bytes):
    record, _replay = store.create_or_resume(
        workspace_id=workspace_id,
        name=name,
        declared_size=len(content),
        claimed_mime="application/octet-stream",
    )
    record = store.append(record.id, offset=0, data=content)
    assert record.state == "ready"
    return record


def test_materialize_resource_succeeds_under_a_long_workspace_root(tmp_path: Path) -> None:
    """Upload -> materialize into a padded workspace: the exact live failure."""

    workspace_root = _padded_root(tmp_path)
    store = ResourceStore(root=tmp_path / "resources", max_resource_bytes=1024 * 1024)
    content = b"%PDF-1.4 minimal long path regression fixture\n"
    record = _ingest_ready_resource(
        store,
        workspace_id="ws_long",
        name="uploaded-1790285681072" + "x" * 40 + ".pdf",
        content=content,
    )

    updated = resource_materialization.materialize_resource(store, record, workspace_root)

    destination = Path(updated.workspace_path)
    assert len(str(destination)) > 260, "fixture didn't actually exceed MAX_PATH"
    assert os.path.isfile(win_extended_path(destination))
    with open(win_extended_path(destination), "rb") as handle:
        assert handle.read() == content

    # The `recorded == destination` idempotent re-materialize early-return must
    # also survive a long path (it stats the destination).
    again = resource_materialization.materialize_resource(store, updated, workspace_root)
    assert again.workspace_path == updated.workspace_path


def test_materialize_resource_staging_temp_name_is_short(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write site actually uses the short helper, not ``<name>.<32 hex>.tmp``."""

    workspace_root = _padded_root(tmp_path)
    store = ResourceStore(root=tmp_path / "resources", max_resource_bytes=1024 * 1024)
    content = b"short-stage-name-fixture"
    record = _ingest_ready_resource(
        store,
        workspace_id="ws_short",
        name="a-fairly-long-original-filename-from-the-user.txt",
        content=content,
    )

    captured: list[str] = []
    original = resource_materialization.short_stage_name

    def _spy(**kwargs: object) -> str:
        name = original(**kwargs)
        captured.append(name)
        return name

    monkeypatch.setattr(resource_materialization, "short_stage_name", _spy)

    resource_materialization.materialize_resource(store, record, workspace_root)

    assert captured, "materialize_resource never called the shared staging helper"
    for name in captured:
        assert len(name) <= 20
        assert "a-fairly-long-original-filename" not in name


def test_remove_materialized_resource_cleans_up_under_a_long_workspace_root(
    tmp_path: Path,
) -> None:
    workspace_root = _padded_root(tmp_path)
    store = ResourceStore(root=tmp_path / "resources", max_resource_bytes=1024 * 1024)
    content = b"delete-me"
    record = _ingest_ready_resource(
        store,
        workspace_id="ws_delete",
        name="deleteme" + "y" * 60 + ".bin",
        content=content,
    )
    updated = resource_materialization.materialize_resource(store, record, workspace_root)
    destination = Path(updated.workspace_path)
    assert os.path.isfile(win_extended_path(destination))

    resource_materialization.remove_materialized_resource(updated)

    assert not os.path.isfile(win_extended_path(destination))
    assert not os.path.isdir(win_extended_path(destination.parent))


# --------------------------------------------------------------------------- #
# artifacts/cas.py — the ingest+verify backbone both call chains share.
# --------------------------------------------------------------------------- #


def test_cas_ingest_identity_under_a_long_workspace_root(tmp_path: Path) -> None:
    workspace_root = _padded_root(tmp_path)
    source = tmp_path / "designated_output.txt"
    content = b"cas ingest long workspace root fixture\n" * 10
    source.write_bytes(content)

    outcome = cas.ingest_identity(source, workspace_root=workspace_root)

    assert outcome.custody == Custody.CAS
    assert outcome.blob_path is not None
    assert len(str(outcome.blob_path)) > 260, "fixture didn't actually exceed MAX_PATH"
    assert os.path.isfile(win_extended_path(outcome.blob_path))
    assert cas.sha256_file(outcome.blob_path) == outcome.evidence.sha256

    # Re-ingesting the SAME bytes must hit the dedup_existing branch cleanly.
    again = cas.ingest_identity(source, workspace_root=workspace_root)
    assert again.reason == "dedup_existing"


def test_cas_sha256_file_reads_a_file_under_a_long_path(tmp_path: Path) -> None:
    workspace_root = _padded_root(tmp_path)
    target = workspace_root / ("hash-me" + "q" * 40 + ".bin")
    content = b"hash me please\n"
    with open(win_extended_path(target), "wb") as handle:
        handle.write(content)
    assert len(str(target)) > 260, "fixture didn't actually exceed MAX_PATH"

    assert cas.sha256_file(target) == hashlib.sha256(content).hexdigest()


# --------------------------------------------------------------------------- #
# documents/store.py — working-copy materialization + manifest persistence.
# --------------------------------------------------------------------------- #


class _FakeWorkspace:
    def __init__(self, root_path: str) -> None:
        self.root_path = root_path


class _FakeWorkspaceStore:
    def __init__(self, workspaces: dict[str, _FakeWorkspace]) -> None:
        self._workspaces = workspaces

    def get(self, workspace_id: str) -> "_FakeWorkspace | None":
        return self._workspaces.get(workspace_id)


def _fake_app(workspace_id: str, workspace_root: Path) -> SimpleNamespace:
    workspaces = _FakeWorkspaceStore({workspace_id: _FakeWorkspace(str(workspace_root))})
    return SimpleNamespace(state=SimpleNamespace(workspaces=workspaces))


def test_atomic_json_round_trips_under_a_long_path(tmp_path: Path) -> None:
    workspace_root = _padded_root(tmp_path)
    target = workspace_root / ("manifest" + "m" * 60) / "manifest.json"
    payload = {"id": "docwc_fixture", "status": "active"}

    document_store_module._atomic_json(target, payload)

    assert len(str(target)) > 260, "fixture didn't actually exceed MAX_PATH"
    assert os.path.isfile(win_extended_path(target))
    with open(win_extended_path(target), "r", encoding="utf-8") as handle:
        assert json.load(handle) == payload


def test_create_working_copy_materializes_under_a_long_workspace_root(tmp_path: Path) -> None:
    workspace_root = _padded_root(tmp_path)
    source = tmp_path / "source.md"
    content = b"# Long path working copy fixture\n"
    source.write_bytes(content)
    sha = cas.sha256_file(source)

    app = _fake_app("ws_docs", workspace_root)
    store = document_store_module.DocumentStore(app)  # type: ignore[arg-type]
    record = ArtifactRecord(workspace_id="ws_docs", name="report" + "z" * 60 + ".md")
    version = ArtifactVersion(
        evidence=IdentityEvidence.hashed_at_use(sha256=sha, size_bytes=len(content)),
        path=str(source),
        custody=Custody.WORKSPACE_REFERENCED,
    )

    row = store.create_working_copy(
        session_id="sess_1",
        workspace_id="ws_docs",
        record=record,
        version=version,
        provider="native",
        writable=True,
        auto_checkpoint=False,
    )

    working_copy_path = Path(row.path)
    assert len(str(working_copy_path)) > 260, "fixture didn't actually exceed MAX_PATH"
    assert os.path.isfile(win_extended_path(working_copy_path))
    with open(win_extended_path(working_copy_path), "rb") as handle:
        assert handle.read() == content
    assert row.base_sha256 == sha

    manifest_path = working_copy_path.parent / "manifest.json"
    assert os.path.isfile(win_extended_path(manifest_path))
    with open(win_extended_path(manifest_path), "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    assert manifest["id"] == row.id
