"""Remote CLIO deploy: adopt-or-stop, the pinned install, status, teardown."""

from __future__ import annotations

import asyncio
import os
import shlex
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
    install_command,
    parse_claim,
    status_command,
    teardown_command,
)
from clio_agent.gact.infrastructure.drivers import (
    CLIO_AGENT_PORT,
    build_driver_plan,
    clio_agent_version,
)
from clio_agent.gact.infrastructure.models import (
    CommandResult,
    CreateTargetRequest,
    ServiceActionRequest,
    ServiceRecord,
    SshRoute,
    TargetFacts,
)
from clio_agent.gact.infrastructure.release_tag import release_tag
from clio_agent.gact.infrastructure.runtime import InfrastructureRuntime
from clio_agent.gact.infrastructure.store import InfrastructureStore

# ---------------------------------------------------------------------------
# Plan and runtime semantics (platform-independent)
# ---------------------------------------------------------------------------

# The remote installs exactly the version this CLIO runs.
VERSION = clio_agent_version()


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


def test_install_claims_the_port_then_installs_this_clios_version(tmp_path: Path) -> None:
    plan = _plan(InfrastructureStore(tmp_path / "infra.json"))
    tags = [spec.args[1].splitlines()[0] for spec in plan.commands]
    assert tags[:2] == ["# clio-deploy:claim", "# clio-deploy:install"]
    assert '"$bin/clio" start' in plan.commands[2].args[1]
    # Trailing "0": claim never replaces a found CLIO unless configuration
    # says so (see build_driver_plan's on_conflict plumbing).
    assert plan.commands[0].args[-4:] == [str(CLIO_AGENT_PORT), VERSION, "0", ""]
    install = plan.commands[1]
    assert install.args[-4:] == [
        VERSION,
        f"https://pypi.org/pypi/clio-agent/{VERSION}/json",
        f"https://raw.githubusercontent.com/iowarp/clio-agent/{release_tag(VERSION)}/install/install.sh",
        release_tag(VERSION),
    ]
    # A failed install fails the step: nothing swallows it.
    assert "; true" not in install.args[1] and "set -o pipefail" in install.args[1]
    assert plan.teardown is not None
    fresh = plan.teardown(ClaimResult(result="free", existing_root=False))
    kept = plan.teardown(ClaimResult(result="stopped", existing_root=True))
    assert fresh.args[1].startswith("# clio-deploy:teardown")
    assert fresh.args[-2] == "1" and kept.args[-2] == "0"


def test_reinstall_claims_the_port_before_uninstalling_this_roots_own_install() -> None:
    """#1528 review, MEDIUM: the claim must run before reinstall's own
    uninstall step (stop + rm -rf), or a found conflict destroys this root's
    install for nothing while the actual conflict on the port stays
    untouched. A conflict must leave the host and this root's record alone.
    """

    plan = build_driver_plan(
        service_id="clio_agent",
        action="reinstall",
        variant_id="released",
        configuration={},
        facts=TargetFacts(target_id="t", label="ares", os="linux", arch="x86_64"),
        target=_target(InfrastructureStore(None)),
    )
    tags = [spec.args[1].splitlines()[0] for spec in plan.commands]
    assert tags[0] == "# clio-deploy:claim"
    assert "rm -rf" not in plan.commands[0].args[1]


def test_a_resolved_root_overrides_the_targets_configured_root_for_lifecycle_commands() -> None:
    """#1528 review, MEDIUM: stop/logs/uninstall must act on a service's
    adopted root (from a `connect` answer under a different root than the
    target's configured one), or they act on the wrong install entirely.
    """

    target = _target(InfrastructureStore(None), install_root="/home/me/.local/share/clio")
    for action in ("status", "stop", "logs", "uninstall"):
        plan = build_driver_plan(
            service_id="clio_agent",
            action=action,
            variant_id="released",
            configuration={},
            facts=TargetFacts(target_id="t", label="ares", os="linux", arch="x86_64"),
            target=target,
            resolved_root="/opt/someone-elses-clio",
        )
        # `root` travels as its own argv element (never interpolated into the
        # script text), so check the exact args, not a substring of args[1].
        assert "/opt/someone-elses-clio" in plan.commands[0].args, action
        assert "/home/me/.local/share/clio" not in plan.commands[0].args, action


def test_parse_claim_reads_the_result_line() -> None:
    assert parse_claim("==> Port 17800 is free\nclio-deploy result=free existing_root=0\n") == (
        ClaimResult(result="free", existing_root=False)
    )
    assert parse_claim("clio-deploy result=adopted existing_root=1 pid=42") == ClaimResult(
        result="adopted", existing_root=True, pid="42"
    )
    assert parse_claim(
        "clio-deploy result=found existing_root=0 pid=99 installed=0.9.4.1"
    ) == ClaimResult(result="found", existing_root=False, pid="99", installed_version="0.9.4.1")
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
        if 'rm -rf -- "$root"' in script:
            return "uninstall"
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


async def _run(
    tmp_path: Path,
    transports: _ScriptedTransports,
    *,
    cancel: bool = False,
    configuration: dict[str, str] | None = None,
    action: str = "install",
    store: InfrastructureStore | None = None,
    target_id: str | None = None,
) -> Any:
    store = store or InfrastructureStore(tmp_path / "infra.json")
    resolved_target_id = target_id or _target(store).id
    runtime = InfrastructureRuntime(store, transports)  # type: ignore[arg-type]
    operation = runtime.start_action(
        "clio_agent",
        ServiceActionRequest(
            target_id=resolved_target_id,
            action=action,  # type: ignore[arg-type]
            variant_id="released",
            configuration=configuration or {},
        ),
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
async def test_a_found_conflict_fails_typed_without_touching_anything(tmp_path: Path) -> None:
    """A healthy but non-matching CLIO is never silently stopped (#1528 requirement 4)."""

    transports = _ScriptedTransports(
        "clio-deploy result=found existing_root=1 pid=321 installed=0.9.4.1\n"
    )
    operation = await _run(tmp_path, transports)
    assert operation.state == "failed"
    assert operation.error == "clio_deploy_version_conflict"
    assert operation.conflict is not None
    assert operation.conflict.installed_version == "0.9.4.1"
    assert operation.conflict.pid == "321"
    assert "install" not in transports.ran and "start" not in transports.ran
    assert "teardown" not in transports.ran


@pytest.mark.asyncio
async def test_on_conflict_connect_adopts_the_found_clio_without_installing(
    tmp_path: Path,
) -> None:
    """The person's "Connect to the running CLIO" answer adopts it as-is."""

    transports = _ScriptedTransports(
        "clio-deploy result=found existing_root=1 pid=321 installed=0.9.4.1\n"
    )
    operation = await _run(tmp_path, transports, configuration={"on_conflict": "connect"})
    assert operation.state == "succeeded"
    assert operation.error is None
    assert operation.conflict is None
    assert "install" not in transports.ran and "start" not in transports.ran
    assert "teardown" not in transports.ran


@pytest.mark.asyncio
async def test_a_past_on_conflict_answer_is_never_replayed_on_a_later_operation(
    tmp_path: Path,
) -> None:
    """#1528 review, HIGH: `on_conflict` is this ONE operation's answer, never
    a persisted fact. A later start must claim fresh and ask again about a
    new mismatch, rather than silently reusing a past "connect"/"replace" --
    which could otherwise kill a different desktop's CLIO the second time
    around.
    """

    store = InfrastructureStore(tmp_path / "infra.json")
    target = _target(store)

    connect_transports = _ScriptedTransports(
        "clio-deploy result=found existing_root=1 pid=321 installed=0.9.4.1 "
        "state=healthy owner=/other\n"
    )
    first = await _run(
        tmp_path,
        connect_transports,
        configuration={"on_conflict": "connect"},
        store=store,
        target_id=target.id,
    )
    assert first.state == "succeeded"
    record = store.service(target.id, "clio_agent")
    assert record is not None
    assert "on_conflict" not in record.configuration

    # A DIFFERENT CLIO is on the port now. Nothing in this request answers
    # the conflict -- it must be asked about again, not silently adopted or
    # replaced because of the past "connect".
    second_transports = _ScriptedTransports(
        "clio-deploy result=found existing_root=1 pid=654 installed=0.9.4.2 "
        "state=healthy owner=/someone-else\n"
    )
    second = await _run(
        tmp_path, second_transports, action="start", store=store, target_id=target.id
    )
    assert second.state == "failed"
    assert second.error == "clio_deploy_version_conflict"
    assert second.conflict is not None
    assert second.conflict.pid == "654"
    assert "start" not in second_transports.ran


@pytest.mark.asyncio
async def test_an_on_conflict_saved_before_this_fix_is_ignored_too(tmp_path: Path) -> None:
    """Defense in depth for #1528 review HIGH: even a record that already
    has `on_conflict` saved (data from before this fix) is never replayed.
    """

    store = InfrastructureStore(tmp_path / "infra.json")
    target = _target(store)
    store.put_service(
        ServiceRecord(
            id=f"{target.id}:clio_agent",
            service_id="clio_agent",
            target_id=target.id,
            variant_id="released",
            configuration={"on_conflict": "replace"},
            state="running",
        )
    )
    transports = _ScriptedTransports(
        "clio-deploy result=found existing_root=1 pid=999 installed=0.9.4.3 "
        "state=healthy owner=/other\n"
    )
    operation = await _run(tmp_path, transports, action="start", store=store, target_id=target.id)
    assert operation.state == "failed"
    assert operation.error == "clio_deploy_version_conflict"
    assert "start" not in transports.ran


@pytest.mark.asyncio
async def test_reinstall_with_a_found_conflict_leaves_this_roots_install_untouched(
    tmp_path: Path,
) -> None:
    """#1528 review, MEDIUM: a conflict found while reinstalling must leave
    the host, and this root's own prior install, untouched -- the claim runs
    before reinstall's uninstall step, not after.
    """

    transports = _ScriptedTransports(
        "clio-deploy result=found existing_root=1 pid=321 installed=0.9.4.1 "
        "state=healthy owner=/other\n"
    )
    operation = await _run(tmp_path, transports, action="reinstall")
    assert operation.state == "failed"
    assert operation.error == "clio_deploy_version_conflict"
    # "probe" is the target-capability check (always first, unrelated to
    # this ordering fix); "claim" must be the only *plan* command that ran.
    assert transports.ran == ["probe", "claim"]


@pytest.mark.asyncio
async def test_connect_to_a_different_root_records_it_on_the_service(tmp_path: Path) -> None:
    """#1528 review, MEDIUM: adopting a CLIO under a different root than the
    target's configured one must record that real root, so a later
    stop/logs/uninstall targets the process actually adopted.
    """

    store = InfrastructureStore(tmp_path / "infra.json")
    target = _target(store)
    transports = _ScriptedTransports(
        "clio-deploy result=found existing_root=0 pid=321 installed=0.9.4.1 "
        "state=healthy owner=/opt/someone-elses-clio\n"
    )
    operation = await _run(
        tmp_path,
        transports,
        configuration={"on_conflict": "connect"},
        store=store,
        target_id=target.id,
    )
    assert operation.state == "succeeded"
    record = store.service(target.id, "clio_agent")
    assert record is not None
    assert record.resolved_root == "/opt/someone-elses-clio"


@pytest.mark.asyncio
async def test_an_unresponsive_found_clio_fails_typed_without_being_killed(
    tmp_path: Path,
) -> None:
    """#1528 review, MEDIUM: a server that never answered its health check is
    never a reason to stop it -- it is reported `found` with `health:
    "unresponsive"`, exactly like a healthy-but-mismatched one, and the
    caller decides.
    """

    transports = _ScriptedTransports(
        "clio-deploy result=found existing_root=1 pid=321 installed=0.9.4.1 "
        "state=unresponsive owner=/other\n"
    )
    operation = await _run(tmp_path, transports)
    assert operation.state == "failed"
    assert operation.error == "clio_deploy_version_conflict"
    assert operation.conflict is not None
    assert operation.conflict.health == "unresponsive"
    assert "isn't answering" in operation.progress
    assert "install" not in transports.ran and "start" not in transports.ran


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

# Binds the port and accepts connections, like a real server, but never
# answers -- a slow/wedged process, not "nothing listening" (#1528 review).
_FAKE_HANGING_SERVER = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import socket, sys, time
    port = int(sys.argv[sys.argv.index("--port") + 1])
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(5)
    while True:
        conn, _addr = server.accept()
        time.sleep(3600)
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


def _fake_install(prefix: Path, version: str = VERSION, server_script: str = _FAKE_SERVER) -> Path:
    bin_dir = prefix / "clio-agent" / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    server = bin_dir / "clio-agent"
    server.write_text(server_script)
    server.chmod(0o755)
    python = bin_dir / "python"
    python.write_text(
        '#!/bin/sh\ncase "$*" in\n'
        f'  *clio_agent.paths*) exec {shlex.quote(sys.executable)} "$@" ;;\n'
        f"  *) echo {version} ;;\nesac\n"
    )
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
    host = socket.gethostname().split(".")[0]
    (prefix / f"clio-server.{host}.pid").write_text(f"{process.pid}\n")
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
    result = _run_script(claim_command(str(tmp_path / "clio"), port, VERSION))
    assert result.returncode == 0, result.stdout
    assert parse_claim(result.stdout) == ClaimResult(result="free", existing_root=False)


@linux_only
def test_claim_asks_about_this_installs_healthy_server_even_at_the_target_version(
    tmp_path: Path,
) -> None:
    prefix = tmp_path / "clio"
    _fake_install(prefix)
    port = _free_port()
    server = _start_clio(prefix, port)
    try:
        result = _run_script(claim_command(str(prefix), port, VERSION))
        assert result.returncode == 0, result.stdout
        assert parse_claim(result.stdout) == ClaimResult(
            result="found",
            existing_root=True,
            pid=str(server.pid),
            owner=str(prefix),
            installed_version=VERSION,
            health="healthy",
        )
        assert f"already running (pid {server.pid}" in result.stdout
        assert _alive(server)
    finally:
        server.kill()


@linux_only
def test_claim_finds_another_installs_healthy_clio_without_stopping_it(tmp_path: Path) -> None:
    """A healthy CLIO under a different install root is reported, never stopped (#1528)."""

    ours = tmp_path / "clio"
    other = tmp_path / "clio-ui-acceptance-0941"
    _fake_install(other)
    port = _free_port()
    running = _start_clio(other, port)
    try:
        result = _run_script(claim_command(str(ours), port, VERSION))
        assert result.returncode == 0, result.stdout
        assert parse_claim(result.stdout) == ClaimResult(
            result="found",
            existing_root=False,
            pid=str(running.pid),
            installed_version=VERSION,
            owner=str(other),
            health="healthy",
        )
        assert f"CLIO {VERSION} is already running (pid {running.pid}, {other})" in result.stdout
        assert _alive(running)
    finally:
        running.kill()


@linux_only
def test_claim_with_replace_stops_another_installs_clio_holding_the_port(tmp_path: Path) -> None:
    """The person's "Replace it" answer (replace=True) is what may stop a found CLIO."""

    ours = tmp_path / "clio"
    other = tmp_path / "clio-ui-acceptance-0941"
    _fake_install(other)
    port = _free_port()
    stale = _start_clio(other, port)
    try:
        result = _run_script(claim_command(str(ours), port, VERSION, replace=True))
        assert result.returncode == 0, result.stdout
        assert parse_claim(result.stdout) == ClaimResult(
            result="stopped", existing_root=False, pid=str(stale.pid), owner=str(other)
        )
        assert f"Stopped an old CLIO (pid {stale.pid}, {other})" in result.stdout
        stale.wait(timeout=5)
    finally:
        if _alive(stale):
            stale.kill()


@linux_only
def test_claim_finds_this_installs_old_version_without_stopping_it(tmp_path: Path) -> None:
    """Even under this exact install root, a version mismatch alone never stops it (#1528)."""

    prefix = tmp_path / "clio"
    _fake_install(prefix, version="0.9.4.1")
    port = _free_port()
    running = _start_clio(prefix, port)
    try:
        result = _run_script(claim_command(str(prefix), port, VERSION))
        assert parse_claim(result.stdout) == ClaimResult(
            result="found",
            existing_root=True,
            pid=str(running.pid),
            installed_version="0.9.4.1",
            owner=str(prefix),
            health="healthy",
        )
        assert _alive(running)
    finally:
        running.kill()


@linux_only
def test_claim_with_replace_stops_this_installs_old_version(tmp_path: Path) -> None:
    prefix = tmp_path / "clio"
    _fake_install(prefix, version="0.9.4.1")
    port = _free_port()
    old = _start_clio(prefix, port)
    try:
        result = _run_script(claim_command(str(prefix), port, VERSION, replace=True))
        assert parse_claim(result.stdout) == ClaimResult(
            result="stopped", existing_root=True, pid=str(old.pid), owner=str(prefix)
        )
        old.wait(timeout=5)
    finally:
        if _alive(old):
            old.kill()


@linux_only
def test_claim_finds_an_unresponsive_clio_without_stopping_it(tmp_path: Path) -> None:
    """A recognized CLIO process that never answers is asked about, never killed (#1528 review).

    A fixed short health-check timeout must never be the thing that decides
    to kill a server: it only decides what to call it (``unresponsive``).
    """

    prefix = tmp_path / "clio"
    _fake_install(prefix, server_script=_FAKE_HANGING_SERVER)
    port = _free_port()
    hung = _start_clio(prefix, port)
    try:
        result = _run_script(claim_command(str(prefix), port, VERSION))
        assert result.returncode == 0, result.stdout
        assert parse_claim(result.stdout) == ClaimResult(
            result="found",
            existing_root=True,
            pid=str(hung.pid),
            installed_version=VERSION,
            owner=str(prefix),
            health="unresponsive",
        )
        assert _alive(hung)
    finally:
        hung.kill()


@linux_only
def test_claim_with_replace_stops_an_unresponsive_clio(tmp_path: Path) -> None:
    prefix = tmp_path / "clio"
    _fake_install(prefix, server_script=_FAKE_HANGING_SERVER)
    port = _free_port()
    hung = _start_clio(prefix, port)
    try:
        result = _run_script(claim_command(str(prefix), port, VERSION, replace=True))
        assert result.returncode == 0, result.stdout
        assert parse_claim(result.stdout) == ClaimResult(
            result="stopped", existing_root=True, pid=str(hung.pid), owner=str(prefix)
        )
        hung.wait(timeout=5)
    finally:
        if _alive(hung):
            hung.kill()


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
        result = _run_script(claim_command(str(tmp_path / "clio"), port, VERSION))
        assert result.returncode == 75
        last = result.stdout.strip().splitlines()[-1]
        assert last.startswith(f"xx Port {port} is used by another program (pid {unrelated.pid}:")
        assert _alive(unrelated)
    finally:
        unrelated.kill()


@linux_only
def test_teardown_stops_the_server_this_deploy_started(tmp_path: Path) -> None:
    prefix = tmp_path / "clio"
    _fake_install(prefix)
    port = _free_port()
    server = _start_clio(prefix, port)
    try:
        result = _run_script(teardown_command(str(prefix), port, purge_root=False))
        assert result.returncode == 0, result.stdout
        assert f"Stopped the CLIO this deploy started (pid {server.pid})" in result.stdout
        server.wait(timeout=5)
        assert not list(prefix.glob("clio-server*.pid"))
        assert prefix.exists()
    finally:
        if _alive(server):
            os.killpg(server.pid, signal.SIGKILL)


@linux_only
def test_claim_checks_health_on_this_node_even_behind_a_site_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A site proxy must never answer a health check of 127.0.0.1 (ares compute nodes)."""

    proxy_port = _free_port()
    proxy = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(proxy_port), "--bind", "127.0.0.1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_listening(proxy_port)
        monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy_port}")
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy_port}")
        prefix = tmp_path / "clio"
        _fake_install(prefix)
        port = _free_port()
        server = _start_clio(prefix, port)
        try:
            # Through the proxy the check would get the proxy's error and the
            # healthy server of this install would be reported "found" (or
            # stopped, with replace) instead of adopted.
            result = _run_script(claim_command(str(prefix), port, VERSION))
            assert parse_claim(result.stdout) == ClaimResult(
                result="found",
                existing_root=True,
                pid=str(server.pid),
                owner=str(prefix),
                installed_version=VERSION,
                health="healthy",
            )
            assert _alive(server)
        finally:
            server.kill()
    finally:
        proxy.kill()


@linux_only
def test_teardown_removes_an_install_this_deploy_created(tmp_path: Path) -> None:
    prefix = tmp_path / "clio"
    _fake_install(prefix)
    result = _run_script(teardown_command(str(prefix), _free_port(), purge_root=True))
    assert result.returncode == 0, result.stdout
    assert f"Removed the install this deploy created ({prefix})" in result.stdout
    assert not prefix.exists()


@linux_only
def test_teardown_with_nothing_started_says_so_and_keeps_unmarked_dirs(tmp_path: Path) -> None:
    prefix = tmp_path / "not-ours"
    prefix.mkdir()
    result = _run_script(teardown_command(str(prefix), _free_port(), purge_root=True))
    assert "Nothing to clean up" in result.stdout
    assert prefix.exists()


_FAKE_CURL = textwrap.dedent(
    """\
    #!/bin/sh
    # Stands in for curl: PyPI answers $FAKE_PYPI_CODE; the installer is $FAKE_INSTALLER.
    for arg in "$@"; do
      case "$arg" in
        *pypi.org/pypi/*) printf '%s' "$FAKE_PYPI_CODE"; exit 0 ;;
        *install/install.sh) cat "$FAKE_INSTALLER"; exit 0 ;;
      esac
    done
    exit 22
    """
)


def _install(
    tmp_path: Path, *, pypi: str, installer: str, version: str = "0.9.4.18"
) -> subprocess.CompletedProcess[str]:
    """Run the install step against a stand-in PyPI and release installer."""

    tools = tmp_path / "tools"
    tools.mkdir()
    for name, body in (("curl", _FAKE_CURL), ("uv", "#!/bin/sh\nexit 0\n")):
        (tools / name).write_text(body)
        (tools / name).chmod(0o755)
    (tmp_path / "installer.sh").write_text(installer)
    # Login shells may replace PATH from /etc/profile. Install these test-only
    # shims after that so this regression can never run the network installer.
    bash_env = tmp_path / "bash-env"
    bash_env.write_text(f"export PATH={shlex.quote(str(tools))}:$PATH\n")
    spec = install_command(str(tmp_path / "clio"), version)
    return subprocess.run(
        [spec.program, *spec.args],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            **os.environ,
            "PATH": f"{tools}:{os.environ['PATH']}",
            "FAKE_PYPI_CODE": pypi,
            "FAKE_INSTALLER": str(tmp_path / "installer.sh"),
            "BASH_ENV": str(bash_env),
        },
    )


@linux_only
def test_an_unpublished_version_fails_the_install_with_one_plain_line(tmp_path: Path) -> None:
    result = _install(tmp_path, pypi="404", installer="echo SHOULD-NOT-RUN\n")
    assert result.returncode == 78
    assert result.stdout.strip().splitlines()[-1] == (
        "CLIO 0.9.4.18 isn't published; deploy from a released CLIO."
    )
    assert "SHOULD-NOT-RUN" not in result.stdout


@linux_only
def test_beta_bootstrap_pins_assets_even_with_the_already_released_installer(
    tmp_path: Path,
) -> None:
    """The old beta installer needs GACT_VERSION as well as the script ref override."""

    result = _install(
        tmp_path,
        pypi="200",
        version="0.9.5b1",
        installer='printf "%s|%s|%s\\n" "$CLIO_VERSION" "$CLIO_INSTALLER_REF" "$GACT_VERSION"\n',
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0.9.5b1|v0.9.5-beta.1|v0.9.5-beta.1"


@linux_only
def test_an_unreachable_pypi_fails_the_install_and_says_so(tmp_path: Path) -> None:
    result = _install(tmp_path, pypi="000", installer="echo SHOULD-NOT-RUN\n")
    assert result.returncode == 75
    assert "Could not reach PyPI to check CLIO 0.9.4.18 (HTTP 000)." in result.stdout


@linux_only
def test_a_published_version_runs_its_own_installer_pinned_inside_the_root(
    tmp_path: Path,
) -> None:
    installer = (
        'printf "%s|%s|%s|%s|%s|%s\\n" "$CLIO_VERSION" "$CLIO_INSTALLER_REF" '
        '"$CLIO_PREFIX" "$CLIO_BIN_DIR" "$UV_CACHE_DIR" "$UV_TOOL_BIN_DIR"\n'
    )
    result = _install(tmp_path, pypi="200", installer=installer)
    assert result.returncode == 0, result.stdout + result.stderr
    root = tmp_path / "clio"
    assert result.stdout.strip().splitlines()[-1] == "|".join(
        [
            "0.9.4.18",
            "v0.9.4.18",
            str(root),
            str(root / "bin"),
            str(root / "uv-cache"),
            str(root / "bin"),
        ]
    )
    assert (root / ".clio-managed-install").is_file()


@linux_only
def test_a_failing_installer_fails_the_install_step(tmp_path: Path) -> None:
    result = _install(tmp_path, pypi="200", installer="echo 'xx disk quota exceeded'\nexit 9\n")
    assert result.returncode == 9
    assert "xx disk quota exceeded" in result.stdout


@linux_only
def test_status_is_running_only_when_the_clio_api_answers(tmp_path: Path) -> None:
    prefix = tmp_path / "clio"
    _fake_install(prefix)
    port = _free_port()
    stopped = _run_script(status_command(str(prefix), port))
    assert stopped.stdout.strip() == "stopped"
    server = _start_clio(prefix, port)
    try:
        running = _run_script(status_command(str(prefix), port))
        assert running.returncode == 0
        assert running.stdout.strip() == "running"
    finally:
        server.kill()
