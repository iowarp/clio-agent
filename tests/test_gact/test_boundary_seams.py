"""#1905: the confined seams attach one boundary diagnosis, and the model reads its message.

The fence is simulated by marking the real composed spawn active (mechanism ``landlock``)
with the workspace as its only write root, and the child prints the very denial a fenced
write produces. Both seams run real processes: the shell through its MCP server, ``run_code``
in a real kernel.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from clio_agent.gact.code_mode import kernels
from clio_agent.gact.code_mode import tool as tool_module
from clio_agent.runtime import sandbox
from clio_agent.tools.execution import tool_workspace_context
from clio_agent.tools.mcp_result_text import result_to_text
from clio_agent.tools.servers.shell_server import shell_server

_SHARED = ("kind", "confidence", "mechanism", "paths", "request_roots", "signature")


@pytest.fixture
def fenced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A workspace whose spawns look fenced to it alone; the denied path is outside it."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(workspace))
    original = sandbox.wrap_confined

    def as_fenced(*args: Any, **kwargs: Any) -> sandbox.ConfinedSpawn:
        confined = original(*args, **kwargs)
        details = {**confined.result.details, "write_roots": [str(workspace)]}
        state = replace(
            confined.result, mechanism=sandbox.MECHANISM_LANDLOCK, active=True, details=details
        )
        return replace(confined, result=state)

    monkeypatch.setattr(sandbox, "wrap_confined", as_fenced)
    return workspace, outside / "copy.csv"


def _raise_denial(denied: Path) -> str:
    return f"raise PermissionError(13, 'Permission denied', {str(denied)!r})"


async def _shell(workspace: Path, command: str) -> tuple[dict[str, Any], str]:
    with tool_workspace_context(str(workspace)):
        async with Client(shell_server) as client:
            result = await client.call_tool("bash", {"command": command, "cwd": str(workspace)})
    text = result_to_text(result)  # what the model reads for an MCP tool
    return json.loads(text), text


@pytest.mark.asyncio
async def test_the_shell_result_carries_the_boundary_and_the_model_reads_it(
    fenced: tuple[Path, Path],
) -> None:
    """SABOTAGE: drop the ``diagnose_confined`` call in ``execute_shell`` -> red."""
    workspace, denied = fenced
    shell, text = await _shell(workspace, f"'{sys.executable}' -c \"{_raise_denial(denied)}\"")
    assert shell["exit_code"] == 1 and "PermissionError" in shell["stderr"]  # unchanged
    boundary = shell["boundary"]
    assert boundary["confidence"] == "confirmed" and boundary["mechanism"] == "landlock"
    assert boundary["paths"] == [str(denied)]
    assert boundary["request_roots"] == [str(denied.parent)]
    # The model reads the MCP result's text, which carries the field and its message.
    assert json.loads(text)["boundary"]["message"] == boundary["message"]
    assert (
        f"Run the command again with access_paths={json.dumps([str(denied.parent)])}"
        in (boundary["message"])
    )


@pytest.mark.asyncio
@pytest.mark.timeout(120)  # starts a real kernel process
async def test_shell_and_run_code_attach_the_same_diagnosis(
    fenced: tuple[Path, Path],
) -> None:
    """SABOTAGE: drop the ``diagnose_confined`` call in ``execute_shell``, or ``_boundary``
    in ``execute_script``, and the seam's ``boundary`` is missing -> red."""
    workspace, denied = fenced
    script = _raise_denial(denied)
    shell, shell_text = await _shell(workspace, f"'{sys.executable}' -c \"{script}\"")
    assert shell["exit_code"] == 1 and "PermissionError" in shell["stderr"]  # unchanged
    try:
        with tool_workspace_context(str(workspace)):
            outcome = tool_module.run_script(_agent(), script, session_id="boundary", scope="main")
    finally:
        kernels.close_session("boundary")
    shell_boundary, script_boundary = shell["boundary"], outcome["boundary"]
    assert {k: shell_boundary[k] for k in _SHARED} == {k: script_boundary[k] for k in _SHARED}
    assert shell_boundary["paths"] == [str(denied)]
    assert shell_boundary["request_roots"] == [str(denied.parent)]
    assert shell_boundary["confidence"] == "confirmed"

    # The message reaches the model's text: the shell's JSON result carries the field, the
    # script's text appends it after its error.
    assert json.loads(shell_text)["boundary"]["message"] == shell_boundary["message"]
    assert "Run the command again with access_paths=" in shell_boundary["message"]
    model_text = tool_module.model_text(outcome)
    assert script_boundary["message"] in model_text
    assert "shell_bash call that declares access_paths=" in script_boundary["message"]
    assert model_text.index("Error:") < model_text.index(script_boundary["message"])


@pytest.mark.asyncio
async def test_a_shell_failure_inside_the_territory_carries_no_boundary(
    fenced: tuple[Path, Path],
) -> None:
    workspace, _ = fenced
    shell, _text = await _shell(
        workspace, f"'{sys.executable}' -c \"{_raise_denial(workspace / 'locked.csv')}\""
    )
    assert shell["exit_code"] == 1 and "boundary" not in shell


def _agent() -> Any:
    from types import SimpleNamespace

    from clio_agent.gact.agents.tool_settings import apply_tool_settings

    agent = SimpleNamespace(tools={}, _clio_tool_servers=frozenset(), _clio_deferred_catalog=None)
    apply_tool_settings(agent, SimpleNamespace(tools=[], mcp_servers={}))
    return agent
