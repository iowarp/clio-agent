"""Qualify real remote stdio fleet replacement without making an inference request.

Run once against a new, explicitly owned state directory on the target host. This
uses the shipped shell MCP, real Agent/workspace/session stores and TurnRunner.
It does not qualify the HTTP transport, packaged executable, or model inference.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def write_blueprint(source: Path, revision: str, *, broken: bool = False) -> None:
    """Write a qualification blueprint invoking the production shell MCP."""
    import yaml

    declaration = {
        "id": "beta3-fleet-qualification",
        "title": "Beta 3 fleet qualification",
        "version": revision,
        "root_expert": "main",
        "mcp_servers": {
            "reloadcheck": {
                "transport": "stdio",
                "command": str(source / "missing-python") if broken else sys.executable,
                "args": [
                    "-c",
                    "from clio_agent.tools.servers.shell_server import shell_server; "
                    "shell_server.run(transport='stdio')",
                ],
                "env": {"CLIO_QUALIFICATION_REVISION": revision},
            }
        },
    }
    source.mkdir(parents=True, exist_ok=True)
    (source / "AGENT.md").write_text(
        "---\n" + yaml.safe_dump(declaration) + "---\nExplicit operator qualification.\n",
        encoding="utf-8",
    )
    (source / "experts").mkdir(exist_ok=True)
    (source / "experts/main.md").write_text(
        "---\nid: main\ntitle: Main\ntier: 1\nmodule:\n  kind: react\n"
        "prompt_id: beta3.fleet\ntools: [reloadcheck_bash]\n---\n"
        "Run only the operator's explicitly requested commands.\n",
        encoding="utf-8",
    )


async def qualify(root: Path) -> dict[str, Any]:
    """Exercise active-turn drain, process replacement and rejected-revision rollback."""
    import psutil

    from clio_agent.agent import ClioAgent
    from clio_agent.config import LMProviderConfig
    from clio_agent.gact.agent_blueprints import (
        install_agent_blueprint,
        update_installed_agent_blueprint,
    )
    from clio_agent.gact.app import build_app
    from clio_agent.gact.blueprint_activation import agent_blueprint_activation_metadata
    from clio_agent.gact.blueprint_install_files import tree_checksum
    from clio_agent.gact.blueprint_reload import apply_blueprint_change
    from clio_agent.tools.execution import tool_blueprint_context, tool_workspace_context

    workspace, source = root / "workspace", root / "blueprint-source"
    print("Imported runtime; preparing the real Agent and isolated Core", flush=True)
    workspace.mkdir()
    write_blueprint(source, "1.0.0")
    installed = install_agent_blueprint(source=str(source), scope="workspace", cwd=workspace)
    row = installed["installed"][0]
    destination = Path(row["root"])
    # No provider credential is read and this closed loopback endpoint must never
    # receive a request. Model construction is needed by the real Agent host only.
    agent = ClioAgent(
        provider_config=LMProviderConfig(
            provider="openai",
            model="gpt-4o-mini",
            api_base="http://127.0.0.1:9/v1",
            api_key="unused-qualification-no-inference",
        )
    )
    print("Agent and Core ready", flush=True)
    app = build_app(agent=agent, sessions_path=root / "agent-home/sessions.json")
    runner = app.state.turn_runner
    runner.bind_loop(asyncio.get_running_loop())
    ws = app.state.workspaces.create(name="Fleet qualification", root_path=str(workspace))
    metadata = agent_blueprint_activation_metadata(
        blueprint_wire=row, install_root=destination, scope="workspace", app=app
    )
    metadata["active_agent_blueprint_path"] = str(destination)
    sessions = [app.state.sessions.create(workspace_id=ws.id, metadata=metadata) for _ in range(2)]
    observations: list[dict[str, Any]] = []
    executors: list[Any] = []

    def call(label: str, *, delay: int = 0) -> dict[str, Any]:
        marker = workspace / f"{label}.json"
        code = (
            "import json,os,time,psutil; from pathlib import Path; "
            "parents=psutil.Process().parents(); "
            "servers=[p for p in parents if any('shell_server.run' in a for a in p.cmdline())]; "
            "assert len(servers)==1, [p.cmdline() for p in parents]; "
            "value={'revision':os.environ['CLIO_QUALIFICATION_REVISION'],"
            "'mcp_pid':servers[0].pid}; "
            f"Path({str(marker)!r}).write_text(json.dumps(value)); "
            f"time.sleep({delay}); print(json.dumps(value))"
        )
        with (
            tool_workspace_context(workspace),
            tool_blueprint_context("beta3-fleet-qualification", destination),
            agent.lease_workspace_fleet(str(workspace)),
        ):
            executor = agent._active_tool_executor()
            executors.append(executor)
            result = executor.call_tool_result(
                "reloadcheck_bash",
                {"command": shlex.join([sys.executable, "-c", code]), "timeout_s": 45},
            )
        value = result.data
        if isinstance(value, str):
            value = json.loads(value)
        if value.get("exit_code") != 0:
            raise RuntimeError(f"Real shell command failed: {value}")
        observed = {"label": label, **json.loads(value["stdout"])}
        observations.append(observed)
        return observed

    async def reload() -> dict[str, Any]:
        return await apply_blueprint_change(
            app,
            lambda: update_installed_agent_blueprint(
                blueprint_id="beta3-fleet-qualification", scope="workspace", cwd=workspace
            ),
            label="Qualification Reload",
            verify_runtime=True,
            target={"scope": "workspace", "workspace_id": ws.id},
        )

    try:
        original = tree_checksum(destination)
        first = runner.spawn(
            asyncio.to_thread(call, "active", delay=10), sid=sessions[0].id, turn_id="before"
        )
        async with asyncio.timeout(60):
            while not (workspace / "active.json").exists():
                if first.done():
                    await first
                await asyncio.sleep(0.05)
        write_blueprint(source, "2.0.0")
        replacement = asyncio.create_task(reload())
        async with asyncio.timeout(15):
            while runner.revision_gate._open.is_set():
                await asyncio.sleep(0.01)
        assert not first.done(), "Reload must overlap the real active command"
        assert tree_checksum(destination) == original
        queued = runner.spawn(
            asyncio.to_thread(call, "queued"), sid=sessions[1].id, turn_id="after"
        )
        result = await asyncio.wait_for(replacement, timeout=120)
        await asyncio.gather(first, queued)
        assert result["operation"]["status"] == "applied", result
        assert [v["revision"] for v in observations] == ["1.0.0", "2.0.0"]
        assert executors[0].closed and executors[0] is not executors[1]
        assert not psutil.pid_exists(observations[0]["mcp_pid"])
        applied_checksum = tree_checksum(destination)
        write_blueprint(source, "3.0.0", broken=True)
        try:
            await reload()
        except ValueError as exc:
            refusal = str(exc)
        else:
            raise AssertionError("Invalid replacement was incorrectly accepted")
        assert tree_checksum(destination) == applied_checksum
        recovered = await asyncio.to_thread(call, "after-refusal")
        assert recovered["revision"] == "2.0.0"
        assert not (workspace / ".clio").exists()
        return {
            "status": "passed",
            "host": platform.node(),
            "head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "observations": observations,
            "operation": result["operation"],
            "invalid_revision_refusal": refusal,
            "retained_checksum": applied_checksum,
            "inference_performed": False,
            "packaged_transport_qualified": False,
            "duration_seconds": round(time.monotonic() - START, 2),
        }
    finally:
        await runner.drain(grace=5)
        await asyncio.to_thread(agent.shutdown)


def main() -> None:
    """Create isolated namespace roots and retain the exact qualification receipt."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--core-port", required=True, type=int)
    args = parser.parse_args()
    if os.name != "posix":
        parser.error("This remote shell qualification currently requires a POSIX host")
    if not 1024 <= args.core_port <= 65530:
        parser.error("--core-port must be 1024..65530")
    listeners = [socket.socket() for _ in range(5)]
    try:
        for offset, listener in enumerate(listeners):
            listener.bind(("127.0.0.1", args.core_port + offset))
    finally:
        for listener in listeners:
            listener.close()
    root = args.state_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    os.environ.pop("CLIO_USER_DIR", None)
    os.environ.update(
        CLIO_AGENT_HOME=str(root / "agent-home"),
        CLIO_CORE_HOME=str(root / "core-home"),
        CLIO_SHARED_HOME=str(root / "shared-home"),
        CLIO_ALLOWED_ROOTS=str(root),
        CLIO_RUNTIME_STATE_DIR=str(root / "core-supervision"),
        CLIO_CORE_PORT=str(args.core_port),
        CLIO_ARC_CTE_RAM_CAPACITY="64MB",
        CLIO_ARC_CTE_FILE_CAPACITY="512MB",
        CLIO_ENV_FILE_LOADED="1",
    )
    print(f"Starting isolated qualification at {root}", flush=True)
    result = asyncio.run(qualify(root))
    (root / "receipt.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


START = time.monotonic()
if __name__ == "__main__":
    main()
