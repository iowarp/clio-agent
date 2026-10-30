"""``@file.h5::/path``: a workspace-file reference to one object inside an HDF5 file."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import h5py
import numpy as np
import pytest

from clio_agent.gact.app import build_app
from clio_agent.gact.context_reference_delivery import _workspace_path_block
from clio_agent.gact.context_references import authorize_context_reference_parts_sync
from clio_agent.gact.types import Part


@pytest.fixture
def world(tmp_path: Path) -> tuple[Any, Any, Path]:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    root = tmp_path / "ws"
    (root / "data").mkdir(parents=True)
    workspace = app.state.workspaces.create(name="ws", root_path=str(root))
    session = app.state.sessions.create(workspace_id=workspace.id, title="t")
    with h5py.File(root / "data" / "run.h5", "w") as h5:
        entry = h5.create_group("entry")
        entry.attrs["NX_class"] = "NXentry"
        data = entry.create_dataset("I", data=np.zeros((3, 4), dtype="f4"))
        data.attrs["units"] = "counts"
        data.attrs["long"] = "x" * 500
    return app, session, root


def _resolve(app: Any, session: Any, ref_id: str) -> Part:
    [part] = authorize_context_reference_parts_sync(
        app, session, [Part(type="context_ref", ref_kind="workspace_file", ref_id=ref_id)]
    )
    return part


def _refusal(app: Any, session: Any, ref_id: str) -> dict[str, Any]:
    with pytest.raises(Exception) as refused:  # noqa: PT011 - HTTPException envelope
        _resolve(app, session, ref_id)
    return getattr(refused.value, "detail", {})["error"]


def test_a_dataset_reference_carries_its_locator_and_summary(world) -> None:
    app, session, root = world

    part = _resolve(app, session, "data/run.h5::/entry/I")

    reference = part.metadata["context_reference"]
    assert part.label == "run.h5 › /entry/I"
    assert reference["navigation"]["path"] == "data/run.h5"
    assert reference["navigation"]["locator"] == "/entry/I"
    summary = reference["delivery"]["object"]
    assert summary["kind"] == "dataset"
    assert summary["shape"] == [3, 4]
    assert summary["dtype"] == "float32"
    assert summary["attributes"]["units"] == "counts"
    assert len(summary["attributes"]["long"]) == 200
    assert reference["delivery"]["agent_host_path"] == str(root / "data" / "run.h5")


def test_the_agent_is_told_which_object_was_meant(world) -> None:
    app, session, _root = world

    part = _resolve(app, session, "data/run.h5::/entry")
    block = _workspace_path_block(part, part.metadata["context_reference"])

    assert "'/entry'" in block
    assert '"NX_class": "NXentry"' in block
    assert '"kind": "group"' in block


def test_a_missing_object_or_an_outside_link_is_a_typed_refusal(world, tmp_path) -> None:
    app, session, root = world
    outside = tmp_path / "secret.h5"
    with h5py.File(outside, "w") as h5:
        h5.create_dataset("s", data=[42])
    with h5py.File(root / "data" / "run.h5", "a") as h5:
        h5["leak"] = h5py.ExternalLink(str(outside), "/")

    missing = _refusal(app, session, "data/run.h5::/entry/nope")
    leak = _refusal(app, session, "data/run.h5::/leak/s")

    assert missing["error"] == "context_ref_invalid"
    assert missing["details"]["reason"] == "hdf5_path_not_found"
    assert leak["error"] == "context_ref_invalid"
    assert leak["details"]["reason"] == "hdf5_link_outside_workspace"


def test_a_file_named_with_double_colons_is_still_a_plain_file(world) -> None:
    app, session, root = world
    odd = root / "a::b.txt"
    odd.write_text("hello", encoding="utf-8")

    part = _resolve(app, session, "a::b.txt")

    assert part.label == "a::b.txt"
    assert "locator" not in part.metadata["context_reference"]["navigation"]
