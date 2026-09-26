"""Remote CLIO deploy: adopt-or-stop on the API port, and teardown on failure/cancel."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact.infrastructure.clio_agent_deploy import (
    ClaimResult,
    claim_command,
    parse_claim,
    teardown_command,
)
from clio_agent.gact.infrastructure.drivers import (
    CLIO_AGENT_PORT,
    CLIO_AGENT_VERSION,
    build_driver_plan,
)
from clio_agent.gact.infrastructure.models import (
    CommandResult,
    CreateTargetRequest,
    ServiceActionRequest,
    SshRoute,
    TargetFacts,
)
from clio_agent.gact.infrastructure.runtime import InfrastructureRuntime
from clio_agent.gact.infrastructure.store import InfrastructureStore

# ---------------------------------------------------------------------------
# Plan and runtime semantics (platform-independent)
# ---------------------------------------------------------------------------


def _target(store: InfrastructureStore, install_root: str = "") -> Any:
    target = store.create_target(
        CreateTargetRequest(
            label="ares", kind="ssh", ssh=SshRoute(profile="ares"), install_root=install_root
        )
    )
    store.set_transport_state(target.id, "connected")
    return store.target(target.id)


def _plan(store: InfrastructureStore) -> Any:
    return build_driver_plan(
        service_id="clio_agent",
        action="install",
        variant_id="released",
        configuration={},
        facts=TargetFacts(target_id="t", label="ares", os="linux", arch="x86_64"),
        target=_target(store),
    )


def test_install_claims_the_port_before_installing(tmp_path: Path) -> None:
    plan = _plan(InfrastructureStore(tmp_path / "infra.json"))
    tags = [spec.args[1].splitlines()[0] if spec.args else "" for spec in plan.commands]
    assert tags[0] == "# clio-deploy:claim"
    assert "install/install.sh" in plan.commands[1].args[1]
    assert plan.commands[2].args[1].rstrip().endswith('"$bin/clio" start')
    assert plan.commands[0].args[-2:] == [str(CLIO_AGENT_PORT), CLIO_AGENT_VERSION]
    assert plan.teardown is not None
    fresh = plan.teardown(ClaimResult(result="free", existing_root=False))
    kept = plan.teardown(ClaimResult(result="stopped", existing_root=True))
    assert fresh.args[1].startswith("# clio-deploy:teardown")
    assert fresh.args[-1] == "1" and kept.args[-1] == "0"


def test_parse_claim_reads_the_result_line() -> None:
    assert parse_claim("==> Port 17800 is free\nclio-deploy result=free existing_root=0\n") == (
        ClaimResult(result="free", existing_root=False)
    )
    assert parse_claim("clio-deploy result=adopted existing_root=1 pid=42") == ClaimResult(
        result="adopted", existing_root=True
    )
    assert parse_claim("==> Installing\n") is None
    assert parse_claim("clio-deploy result=cleaned") is None


class _ScriptedTransports:
    """Answers each plan step by its tag, recording what ran."""

    def __init__(self, claim: str, *, fail_on: str | None = None, hang_on: str | None = None):
        self.claim = claim
        self.fail_on = fail_on
        self.hang_on = hang_on
        self.ran: list[str] = []

    @staticmethod
    def _step(spec: Any) -> str:
        script = spec.args[1] if len(spec.args) > 1 else ""
        if spec.program == "sh":
            return "probe"
        if script.startswith("# clio-deploy:"):
            return script.splitlines()[0].removeprefix("# clio-deploy:")
        if "install/install.sh" in script:
            return "install"
        if '"$bin/clio" start' in script:
            return "start"
        return "other"

    async def execute(self, target_id: str, spec: Any) -> CommandResult:
        del target_id
        step = self._step(spec)
        self.ran.append(step)
        if step == "probe":
            return CommandResult(exit_code=0, stdout="Linux|x86_64|none|0|0|1\n")
        if step == self.hang_on:
            await asyncio.sleep(60)
        if step == self.fail_on:
            return CommandResult(exit_code=1, stdout="", stderr="xx install failed")
        if step == "claim":
            return CommandResult(exit_code=0, stdout=self.claim)
        return CommandResult(exit_code=0, stdout="ok\n")

    async def forward(self, target_id: str, remote_port: int, preferred: int | None = None) -> str:
        del target_id, preferred
        return f"http://127.0.0.1:{remote_port + 1}"


async def _run(tmp_path: Path, transports: _ScriptedTransports, *, cancel: bool = False) -> Any:
    store = InfrastructureStore(tmp_path / "infra.json")
    target = _target(store)
    runtime = InfrastructureRuntime(store, transports)  # type: ignore[arg-type]
    operation = runtime.start_action(
        "clio_agent",
        ServiceActionRequest(target_id=target.id, action="install", variant_id="released"),
    )
    if cancel:
        for _ in range(200):
            if transports.hang_on in transports.ran:
                break
            await asyncio.sleep(0.01)
        return await runtime.cancel(operation.id)
    for _ in range(500):
        current = store.operation(operation.id)
        if current is not None and current.state in {"succeeded", "failed", "cancelled"}:
            return current
        await asyncio.sleep(0.01)
    raise AssertionError("operation did not finish")


@pytest.mark.asyncio
async def test_adopting_this_installs_server_skips_install_and_start(tmp_path: Path) -> None:
    transports = _ScriptedTransports("clio-deploy result=adopted existing_root=1 pid=7\n")
    operation = await _run(tmp_path, transports)
    assert operation.state == "succeeded"
    assert "install" not in transports.ran and "start" not in transports.ran
    assert "teardown" not in transports.ran


@pytest.mark.asyncio
async def test_a_failure_after_the_claim_tears_down_what_the_deploy_started(
    tmp_path: Path,
) -> None:
    transports = _ScriptedTransports(
        "clio-deploy result=stopped existing_root=0 pid=1036897\n", fail_on="start"
    )
    operation = await _run(tmp_path, transports)
    assert operation.state == "failed"
    assert transports.ran[-1] == "teardown"
    assert operation.progress == "Failed. Cleaned up what this deploy started."


@pytest.mark.asyncio
async def test_cancel_mid_install_tears_down_before_reporting(tmp_path: Path) -> None:
    transports = _ScriptedTransports("clio-deploy result=free existing_root=0\n", hang_on="install")
    operation = await _run(tmp_path, transports, cancel=True)
    assert operation.state == "cancelled"
    assert transports.ran[-1] == "teardown"
    assert "Cleaned up" in operation.progress


@pytest.mark.asyncio
async def test_a_failed_claim_started_nothing_and_tears_nothing_down(tmp_path: Path) -> None:
    transports = _ScriptedTransports("", fail_on="claim")
    operation = await _run(tmp_path, transports)
    assert operation.state == "failed"
    assert "teardown" not in transports.ran


# ---------------------------------------------------------------------------
# The scripts themselves, against real processes on a fake occupied port
# ---------------------------------------------------------------------------

linux_only = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("bash") is None,
    reason="the deploy scripts run on the remote Linux host (/proc, ss/lsof)",
)

_FAKE_SERVER = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import http.server, sys
    port = int(sys.argv[sys.argv.index("--port") + 1])
    class Health(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        def log_message(self, *args):
            pass
    http.server.ThreadingHTTPServer(("127.0.0.1", port), Health).serve_forever()
    """
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_listening(port: int) -> None:
    for _ in range(100):
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise AssertionError(f"nothing listens on {port}")


def _fake_install(prefix: Path, version: str = CLIO_AGENT_VERSION) -> Path:
    bin_dir = prefix / "clio-agent" / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    server = bin_dir / "clio-agent"
    server.write_text(_FAKE_SERVER)
    server.chmod(0o755)
    python = bin_dir / "python"
    python.write_text(f"#!/bin/sh\necho {version}\n")
    python.chmod(0o755)
    (prefix / ".clio-managed-install").touch()
    return server


def _start_clio(prefix: Path, port: int) -> subprocess.Popen[bytes]:
    """Start a fake CLIO server exactly the way the launcher does."""

    server = prefix / "clio-agent" / ".venv" / "bin" / "clio-agent"
    process = subprocess.Popen(
        [sys.executable, str(server), "serve", "--port", str(port)],
        cwd=prefix / "clio-agent",
        start_new_session=True,
    )
    (prefix / "clio-server.pid").write_text(f"{process.pid}\n")
    _wait_listening(port)
    return process


def _run_script(spec: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [spec.program, *spec.args], capture_output=True, text=True, timeout=spec.timeout_seconds
    )


def _alive(process: subprocess.Popen[bytes]) -> bool:
    return process.poll() is None


@linux_only
def test_claim_on_a_free_port_reports_free(tmp_path: Path) -> None:
    port = _free_port()
    result = _run_script(claim_command(str(tmp_path / "clio"), "", port, CLIO_AGENT_VERSION))
    assert result.returncode == 0, result.stdout
    assert parse_claim(result.stdout) == ClaimResult(result="free", existing_root=False)


@linux_only
def test_claim_adopts_this_installs_healthy_server_of_the_target_version(tmp_path: Path) -> None:
    prefix = tmp_path / "clio"
    _fake_install(prefix)
    port = _free_port()
    server = _start_clio(prefix, port)
    try:
        result = _run_script(claim_command(str(prefix), "", port, CLIO_AGENT_VERSION))
        assert result.returncode == 0, result.stdout
        assert parse_claim(result.stdout) == ClaimResult(result="adopted", existing_root=True)
        assert f"Reusing the running CLIO (pid {server.pid}" in result.stdout
        assert _alive(server)
    finally:
        server.kill()


@linux_only
def test_claim_stops_another_installs_clio_holding_the_port(tmp_path: Path) -> None:
    ours = tmp_path / "clio"
    other = tmp_path / "clio-ui-acceptance-0941"
    _fake_install(other)
    port = _free_port()
    stale = _start_clio(other, port)
    try:
        result = _run_script(claim_command(str(ours), "", port, CLIO_AGENT_VERSION))
        assert result.returncode == 0, result.stdout
        assert parse_claim(result.stdout) == ClaimResult(result="stopped", existing_root=False)
        assert f"Stopped an old CLIO (pid {stale.pid}, {other})" in result.stdout
        stale.wait(timeout=5)
    finally:
        if _alive(stale):
            stale.kill()


@linux_only
def test_claim_stops_this_installs_old_version(tmp_path: Path) -> None:
    prefix = tmp_path / "clio"
    _fake_install(prefix, version="0.9.4.1")
    port = _free_port()
    old = _start_clio(prefix, port)
    try:
        result = _run_script(claim_command(str(prefix), "", port, CLIO_AGENT_VERSION))
        assert parse_claim(result.stdout) == ClaimResult(result="stopped", existing_root=True)
        old.wait(timeout=5)
    finally:
        if _alive(old):
            old.kill()


@linux_only
def test_claim_never_touches_an_unrelated_program_on_the_port(tmp_path: Path) -> None:
    port = _free_port()
    unrelated = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_listening(port)
        result = _run_script(claim_command(str(tmp_path / "clio"), "", port, CLIO_AGENT_VERSION))
        assert result.returncode == 75
        last = result.stdout.strip().splitlines()[-1]
        assert last.startswith(f"xx Port {port} is used by another program (pid {unrelated.pid}:")
        assert _alive(unrelated)
    finally:
        unrelated.kill()


@linux_only
def test_teardown_stops_the_server_and_runtime_this_deploy_started(tmp_path: Path) -> None:
    prefix = tmp_path / "clio"
    _fake_install(prefix)
    port = _free_port()
    server = _start_clio(prefix, port)
    runtime_state = prefix / "runtime-state"
    runtime_state.mkdir()
    core = subprocess.Popen(["bash", "-c", "exec -a clio_run sleep 60"], start_new_session=True)
    (runtime_state / "clio-runtime.pid").write_text(f"{core.pid} 0\n")
    try:
        result = _run_script(teardown_command(str(prefix), "", port, purge_root=False))
        assert result.returncode == 0, result.stdout
        assert f"Stopped the CLIO this deploy started (pid {server.pid})" in result.stdout
        assert f"Stopped its clio-core runtime (pid {core.pid})" in result.stdout
        server.wait(timeout=5)
        core.wait(timeout=5)
        assert not (prefix / "clio-server.pid").exists()
        assert prefix.exists()
    finally:
        for process in (server, core):
            if _alive(process):
                os.killpg(process.pid, signal.SIGKILL)


@linux_only
def test_teardown_removes_an_install_this_deploy_created(tmp_path: Path) -> None:
    prefix = tmp_path / "clio"
    _fake_install(prefix)
    result = _run_script(teardown_command(str(prefix), "", _free_port(), purge_root=True))
    assert result.returncode == 0, result.stdout
    assert f"Removed the install this deploy created ({prefix})" in result.stdout
    assert not prefix.exists()


@linux_only
def test_teardown_with_nothing_started_says_so_and_keeps_unmarked_dirs(tmp_path: Path) -> None:
    prefix = tmp_path / "not-ours"
    prefix.mkdir()
    result = _run_script(teardown_command(str(prefix), "", _free_port(), purge_root=True))
    assert "Nothing to clean up" in result.stdout
    assert prefix.exists()
