"""H3: a server declares which tool arguments are paths and on which machine they live.

``_meta["ai.iowarp.clio/paths"]`` on a tool definition (:mod:`clio_agent.tools.tool_path_contract`)
replaces a guess from the argument's name: a successful call that opens a file on another
machine records where the input lives and is not shown as degraded provenance.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from fastmcp import FastMCP

from clio_agent.gact.app import build_app
from clio_agent.gact.artifacts.declared_path_edges import REMOTE_INPUT_NOTE, declared_path_scan
from clio_agent.gact.artifacts.observer_provenance import tool_provenance_metadata
from clio_agent.gact.artifacts.transform_edges import detect_used_edges
from clio_agent.gact.artifacts.transforms import record_transform
from clio_agent.gact.runtime.globals import _tool_session_context
from clio_agent.tools import tool_path_contract as contract
from tests.test_gact.test_artifacts_s5 import _make_app

KEY = contract.PATHS_META_KEY
REMOTE_READ = {"path": {"machine": "remote", "access": "read", "host": "node08"}}


def _bind(tool_name: str, declaration: dict[str, Any] | None) -> None:
    meta = {KEY: declaration} if declaration is not None else {}
    contract.bind_call(tool_name, {"name": tool_name, "_meta": meta})


# --------------------------------------------------------------------------- #
# The declaration                                                              #
# --------------------------------------------------------------------------- #


def test_a_fastmcp_tool_definition_declares_its_paths() -> None:
    server = FastMCP("viewer")

    @server.tool(meta={KEY: {**REMOTE_READ, "out": {"machine": "local", "access": "write"}}})
    def open_view(path: str, out: str = "") -> str:
        return path

    import asyncio

    tool = asyncio.run(server.get_tool("open_view"))
    declared = contract.declared_paths(tool, "open_view")
    assert declared["path"] == contract.DeclaredPath("remote", "read", "node08")
    assert declared["out"] == contract.DeclaredPath("local", "write", "")


@pytest.mark.parametrize(
    ("declaration", "detail"),
    [
        ("path", "declaration_not_an_object"),
        ({"path": "remote"}, "entry_not_an_object"),
        ({"path": {"machine": "elsewhere"}}, "machine_unknown"),
        ({"path": {"machine": "local", "access": "append"}}, "access_unknown"),
        ({"path": {"machine": "remote", "host": "a\nb"}}, "host_invalid"),
    ],
)
def test_a_malformed_declaration_is_ignored_with_a_typed_reason(
    declaration: Any, detail: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger=contract.__name__):
        declared = contract.declared_paths({"_meta": {KEY: declaration}}, "t")
    assert dict(declared) == {}
    assert "reason=path_declaration_invalid" in caplog.text and detail in caplog.text


def test_the_binding_answers_only_for_the_bound_tool() -> None:
    _bind("viewer_open_view", REMOTE_READ)
    assert contract.declared_args("viewer_open_view", machine="remote") == {"path"}
    assert contract.declared_args("viewer_open_view", machine="local") == frozenset()
    assert contract.declared_args("other_tool") == frozenset()
    _bind("other_tool", None)  # the next call rebinds
    assert contract.declared_args("viewer_open_view") == frozenset()


# --------------------------------------------------------------------------- #
# Provenance                                                                   #
# --------------------------------------------------------------------------- #


def _scan(tmp_path: Path, tool_name: str, args: dict[str, Any], *, ok: bool = True) -> Any:
    root = tmp_path / "workspace"
    root.mkdir(exist_ok=True)
    app, session, _ = _make_app(root)
    return detect_used_edges(
        app,
        session.id,
        args=args,
        workspace_id="ws1",
        turn_id="",
        trace_id="",
        tool_name=tool_name,
        allow_external_inputs=ok,
    )


def test_an_undeclared_path_argument_still_warns(tmp_path: Path) -> None:
    _bind("viewer_open_view", None)
    scan = _scan(tmp_path, "viewer_open_view", {"path": "/home/researcher/data/wavelet.vti"})
    assert [note["reason"] for note in scan.notes] == ["external_input_contract_unknown"]


def test_a_declared_remote_read_is_a_remote_input_not_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a path on another machine is never looked up here")

    _bind("viewer_open_view", REMOTE_READ)
    monkeypatch.setattr("clio_agent.gact.artifacts.minting.compute_identity", forbidden)
    scan = _scan(tmp_path, "viewer_open_view", {"path": "/home/researcher/data/wavelet.vti"})
    assert scan.notes == []
    assert len(scan.edges) == 1
    edge = scan.edges[0]
    assert edge.external_ref == "remote:node08:/home/researcher/data/wavelet.vti"
    assert (edge.name, edge.arg, edge.note) == ("wavelet.vti", "path", REMOTE_INPUT_NOTE)
    assert edge.evidence.value == "schema-arg" and not edge.sha256


def test_a_relative_remote_path_never_matches_a_local_workspace_file(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "data.vti").write_bytes(b"local bytes, not the remote file")
    _bind("viewer_open_view", {"path": {"machine": "remote"}})
    scan = _scan(tmp_path, "viewer_open_view", {"path": "data.vti"})
    assert [edge.external_ref for edge in scan.edges] == ["remote:data.vti"]
    assert not any(edge.artifact_id or edge.sha256 for edge in scan.edges)


def test_a_failed_remote_open_records_no_input_and_no_warning(tmp_path: Path) -> None:
    _bind("viewer_open_view", REMOTE_READ)
    scan = _scan(tmp_path, "viewer_open_view", {"path": "/data/x.vti"}, ok=False)
    assert scan.edges == [] and scan.notes == []


def test_a_declared_local_read_outside_the_workspace_is_an_external_input(
    tmp_path: Path,
) -> None:
    _bind("viewer_open_view", {"path": {"machine": "local"}})
    outside = tmp_path / "data" / "wavelet.vti"
    scan = _scan(tmp_path, "viewer_open_view", {"path": str(outside)})
    assert scan.notes == []
    assert [(edge.external_ref, edge.note) for edge in scan.edges] == [
        (f"external:{outside}", "external_input_not_hashed")
    ]


@pytest.mark.parametrize(
    "entry",
    [
        {"machine": "none"},
        {"machine": "local", "access": "write"},
        {"machine": "remote", "access": "write"},
    ],
)
def test_declared_non_inputs_record_nothing(tmp_path: Path, entry: dict[str, str]) -> None:
    _bind("viewer_export", {"path": entry})
    scan = _scan(tmp_path, "viewer_export", {"path": str(tmp_path / "elsewhere" / "fig.png")})
    assert scan.edges == [] and scan.notes == []


def test_the_scan_names_the_args_the_local_scan_skips() -> None:
    _bind(
        "t",
        {
            "a": {"machine": "local"},
            "b": {"machine": "remote"},
            "c": {"machine": "none"},
            "d": {"machine": "local", "access": "write"},
        },
    )
    scan = declared_path_scan("t", {"b": ["/x/1", "/x/1", "/x/2", 3]})
    assert scan.skip == {"b", "c", "d"}
    assert [edge.external_ref for edge in scan.edges] == ["remote:/x/1", "remote:/x/2"]


def test_the_transform_record_carries_the_remote_input_and_no_warning(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    app, session, _ = _make_app(root)
    _bind("viewer_open_view", REMOTE_READ)
    record = record_transform(
        app,
        session.id,
        tool_name="viewer_open_view",
        args={"path": "/home/researcher/data/wavelet.vti"},
        call_id="open-1",
        ok=True,
        result={},
        minted=[],
        workspace_id="ws1",
    )
    assert record is not None
    assert [edge.external_ref for edge in record.used] == [
        "remote:node08:/home/researcher/data/wavelet.vti"
    ]
    assert tool_provenance_metadata(record) == {}  # no warning: the row is not degraded


# --------------------------------------------------------------------------- #
# Through the real execution boundary and tool observer                        #
# --------------------------------------------------------------------------- #


def test_a_successful_remote_open_is_not_shown_degraded(tmp_path: Path) -> None:
    """The execution boundary binds the called tool's declaration, the observer's transform
    record honours it, and the tool row keeps the server's own status. The undeclared twin
    still shows the warning. SABOTAGE: drop ``bind_call`` in ``execution.py`` -> red."""
    from clio_agent.gact.tool_observer import _make_tool_observer
    from clio_agent.tools.execution import (
        SyncMCPToolExecutor,
        ToolRuntimeHooks,
        set_tool_runtime_fallback,
        set_tool_runtime_resolver,
    )

    root = tmp_path / "workspace"
    root.mkdir()
    server = FastMCP("viewer")

    @server.tool(meta={KEY: REMOTE_READ})
    def open_view(path: str) -> str:
        """Open a dataset on the machine the viewer runs on."""
        return f"opened {path}"

    @server.tool()
    def open_undeclared(path: str) -> str:
        """The same tool, declaring nothing."""
        return f"opened {path}"

    remote = "/home/researcher/data/wavelet.vti"
    with TestClient(build_app(sessions_path=tmp_path / "s.json")) as client:
        app = client.app
        wid = client.post("/v1/workspaces", json={"name": "w", "root_path": str(root)}).json()["id"]
        sid = client.post("/v1/sessions", json={"workspace_id": wid}).json()["id"]
        set_tool_runtime_resolver(None)
        set_tool_runtime_fallback(ToolRuntimeHooks(tool_observer=_make_tool_observer(app)))
        executor = SyncMCPToolExecutor(server, timeout=10.0)
        try:
            with _tool_session_context(sid):  # the observer's session; no app loop
                executor.call_tool("open_view", {"path": remote})
                executor.call_tool("open_undeclared", {"path": remote})
        finally:
            executor.close()
            set_tool_runtime_fallback(ToolRuntimeHooks())
        results = [p for p in app.state.live_assistant_parts[sid] if p.type == "tool_result"]
    declared, undeclared = results
    assert "provenance_warnings" not in declared.metadata
    assert (declared.presentation or {}).get("status") != "degraded"
    assert undeclared.metadata["provenance_warnings"][0]["reason"] == (
        "external_input_contract_unknown"
    )
    assert undeclared.presentation["status"] == "degraded"
