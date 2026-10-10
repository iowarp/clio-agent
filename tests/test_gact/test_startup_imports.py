"""The HTTP shell must not initialize unused storage providers or tool servers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.timeout(60)  # A fresh interpreter exercises the real import graph.
def test_http_startup_leaves_unused_providers_and_servers_unloaded(tmp_path: Path) -> None:
    env = {
        **os.environ,
        "CLIO_AGENT_HOME": str(tmp_path / "agent"),
        "CLIO_RUNTIME_STATE_DIR": str(tmp_path / "core"),
        "CLIO_ENV_FILE_LOADED": "1",
    }
    code = """
import json, sys
from clio_agent.gact.app import build_app
app = build_app()
from clio_agent.gact.server_boot import reconcile_connected_storage
reconcile_connected_storage(app)
for name in (
    'googleapiclient.discovery', 'gdrive_fsspec',
    'clio_agent.tools.servers.fs_server', 'clio_agent.tools.servers.shell_server',
    'clio_agent.tools.relay_transport',
    'fastmcp.client.client', 'clio_agent.tools.task_receipt',
):
    assert name not in sys.modules, name
print(json.dumps({'routes': len(app.routes)}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=50
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["routes"] > 300


def test_protocol_metadata_matches_the_installed_sdk() -> None:
    from fastmcp.apps.config import UI_EXTENSION_ID
    from fastmcp.utilities.mime import UI_MIME_TYPE
    from fastmcp.utilities.tasks import TASKS_EXTENSION_ID

    from clio_agent.tools import mcp_extension_registry as registry

    assert registry.UI_EXTENSION_ID == UI_EXTENSION_ID
    assert registry.MCP_APP_MIME_TYPE == UI_MIME_TYPE
    assert registry.TASKS_EXTENSION_ID == TASKS_EXTENSION_ID


def test_standalone_file_policy_does_not_import_the_tool_execution_stack(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from clio_agent.tools.file_policy import FileAccessPolicy; "
            "FileAccessPolicy.from_env(); "
            "assert 'clio_agent.tools.execution' not in sys.modules; "
            "assert 'fastmcp' not in sys.modules",
        ],
        env={**os.environ, "CLIO_ALLOWED_ROOTS": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
