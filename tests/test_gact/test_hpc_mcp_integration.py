"""Installed CLIO Kit acceptance through Agent's production MCP boundary.

This test deliberately uses the candidate console launcher and real stdio MCP
subprocesses.  Only the CSV contents are a test fixture; tool results, the PNG,
artifact registration, and provenance records all come from production code.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.runtime.globals import _gact_app_context, _tool_session_context
from clio_agent.gact.tool_observer import _make_tool_observer
from clio_agent.tools.execution import create_sync_tool_executor, tool_workspace_context
from clio_agent.tools.gateway import build_gateway, namespace_proxies
from clio_agent.tools.mcp_config import MCPServerSpec
from clio_agent.tools.mcp_discovery import discover_declared_tools_bounded

_NAMESPACES = ("ndp", "geo", "pandas", "plot")

pytestmark = pytest.mark.integration


def _checkpoint(stage: str) -> None:
    """Emit bounded live progress for the real subprocess acceptance run."""

    print(f"KIT_AGENT_ACCEPTANCE {stage}", flush=True)


def _workspace_session(client: TestClient, root: Path) -> tuple[str, str]:
    """Create the real workspace and session used by the observer."""

    workspace = client.post(
        "/v1/workspaces", json={"name": "candidate kit acceptance", "root_path": str(root)}
    )
    assert workspace.status_code == 201, workspace.text
    workspace_id = workspace.json()["id"]
    session = client.post("/v1/sessions", json={"workspace_id": workspace_id})
    assert session.status_code == 200, session.text
    return workspace_id, session.json()["id"]


def test_candidate_kit_executes_and_persists_external_input_lineage(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Exercise four real namespaces and persist plot lineage to an external CSV."""

    configured_launcher = os.environ.get("CLIO_TEST_KIT_LAUNCHER", "")
    assert configured_launcher, (
        "CLIO_TEST_KIT_LAUNCHER must name the installed candidate clio-kit launcher"
    )
    candidate_launcher = Path(configured_launcher).resolve()
    assert candidate_launcher.is_file(), (
        f"CLIO_TEST_KIT_LAUNCHER is not a file: {candidate_launcher}"
    )
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    external_csv = tmp_path / "external-source.csv"
    external_csv.write_text("x,y\n0,1\n1,3\n2,5\n", encoding="utf-8")
    output_png = workspace_root / "candidate-line-plot.png"

    specs = {
        namespace: MCPServerSpec(
            name=namespace,
            transport="stdio",
            command=str(candidate_launcher),
            args=("mcp-server", namespace),
        )
        for namespace in _NAMESPACES
    }
    discovery = discover_declared_tools_bounded(specs)
    assert not discovery.degraded, discovery.degraded
    gateway = build_gateway(specs, cwd=str(workspace_root))
    _checkpoint("gateway_mounted")
    app = build_app(sessions_path=tmp_path / "sessions.json")

    with TestClient(app) as client:
        _checkpoint("app_started")
        workspace_id, session_id = _workspace_session(client, workspace_root)
        observer = _make_tool_observer(app)
        app.state.pending_tool_observer = observer
        # The fixture represents an already-approved local science workflow; keep
        # the production gate seam installed while avoiding an interactive prompt.
        app.state.pending_permission_gate = lambda *_args, **_kwargs: "allow"
        with (
            _gact_app_context(app),
            _tool_session_context(session_id),
            tool_workspace_context(workspace_root),
            create_sync_tool_executor(
                gateway,
                timeout=90.0,
                setup_timeout=90.0,
                preloaded_tools=discovery.tools,
                namespace_servers=namespace_proxies(gateway),
            ) as executor,
        ):
            _checkpoint("discovery_complete")
            tool_names = set(executor.get_tool_names())
            assert {
                "ndp_search_datasets",
                "geo_geocode",
                "pandas_profile_csv",
                "plot_line_plot",
            } <= tool_names

            try:
                _checkpoint("plot_call_started")
                plot_result = executor.call_tool_result(
                    "plot_line_plot",
                    {
                        "file_path": str(external_csv),
                        "x_column": "x",
                        "y_column": "y",
                        "output_path": str(output_png),
                    },
                )
                _checkpoint("plot_call_complete")
                _checkpoint("profile_call_started")
                profile_result = executor.call_tool_result(
                    "pandas_profile_csv", {"data_path": str(external_csv)}
                )
                _checkpoint("profile_call_complete")
            finally:
                _checkpoint("executor_close_started")
        _checkpoint("executor_closed")

        assert profile_result.is_error is False
        assert plot_result.is_error is False
        assert profile_result.structured_content is not None
        assert plot_result.structured_content is not None
        assert "reason=tool_observer_failed" not in caplog.text
        assert output_png.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

        registry = get_registry(app)
        match = registry.find_version_by_path(workspace_id, str(output_png))
        assert match is not None, json.dumps(registry.summary(), default=str)
        _record, version = match
        # Drop the live projection so both HTTP reads below must fold the durable
        # provenance ledger rather than succeeding from the observer's memory.
        del app.state.artifact_registry

        transforms = client.get(f"/v1/sessions/{session_id}/transforms")
        assert transforms.status_code == 200, transforms.text
        rows = transforms.json()["transforms"]
        profile = next(row for row in rows if row["instrument"]["tool"] == "pandas_profile_csv")
        plot = next(row for row in rows if row["instrument"]["tool"] == "plot_line_plot")
        assert any(
            edge.get("external_ref") == f"external:{external_csv}"
            and edge.get("evidence") == "schema-arg"
            for edge in profile["used"]
        )
        assert any(
            edge.get("external_ref") == f"external:{external_csv}"
            and edge.get("evidence") == "schema-arg"
            for edge in plot["used"]
        )
        assert any(edge.get("artifact_id") == version.artifact_id for edge in plot["generated"])

        lineage = client.get(
            f"/v1/artifacts/{version.artifact_id}/lineage",
            params={"direction": "upstream", "depth": 4},
        )
        assert lineage.status_code == 200, lineage.text
        graph = lineage.json()
        assert any(
            node.get("external") is True
            and node.get("id") == f"external:{external_csv}"
            and node.get("name") == external_csv.name
            for node in graph["nodes"]
        )
        assert any(edge.get("type") == "used" for edge in graph["edges"])
        assert any(edge.get("type") == "generated" for edge in graph["edges"])
        _checkpoint("api_assertions_complete")
