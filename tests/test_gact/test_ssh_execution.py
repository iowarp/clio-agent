"""Shared SSH execution uses real encrypted channels and the actual approval gate."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import paramiko
import pytest
from fastapi import FastAPI
from paramiko.common import (
    AUTH_FAILED,
    AUTH_SUCCESSFUL,
    OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED,
    OPEN_SUCCEEDED,
)

from clio_agent.gact import ssh_tools
from clio_agent.gact.agents.native_tool_registry import build_native_tools
from clio_agent.gact.infrastructure import ssh_execution
from clio_agent.gact.infrastructure.models import SshRoute
from clio_agent.gact.tool_result_presentation import ToolPresentation


@pytest.fixture
def ssh_peer(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Authenticate and exchange actual SSH command/exit/output frames on loopback."""
    key = paramiko.RSAKey.generate(2048)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    commands: list[str] = []
    stopped = threading.Event()
    transports: list[paramiko.Transport] = []

    class Server(paramiko.ServerInterface):
        def check_auth_password(self, username: str, password: str) -> int:
            return AUTH_SUCCESSFUL if (username, password) == ("test", "test") else AUTH_FAILED

        def get_allowed_auths(self, username: str) -> str:
            return "password"

        def check_channel_request(self, kind: str, chanid: int) -> int:
            return OPEN_SUCCEEDED if kind == "session" else OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

        def check_channel_exec_request(self, channel: paramiko.Channel, command: bytes) -> bool:
            text = command.decode()
            commands.append(text)
            if text == "no-ack":
                stopped.wait(3)
                return False

            def respond() -> None:
                # Wait for the client's stdin EOF: exec success must be sent
                # before an immediately finishing server closes the channel.
                channel.recv(1)
                if text == "wait":
                    stopped.wait(3)
                elif text == "large":
                    channel.sendall(b"science\n" * 10000)
                    channel.sendall_stderr(b"diagnostic\n" * 10000)
                    channel.send_exit_status(0)
                elif text == "empty":
                    channel.send_exit_status(0)
                elif text == "unknown-exit":
                    pass
                else:
                    channel.sendall(b"remote science\n")
                    channel.sendall_stderr(b"instrument note\n")
                    channel.send_exit_status(7 if text == "fail" else 0)
                channel.close()

            threading.Thread(target=respond, daemon=True).start()
            return True

    def serve() -> None:
        connection, _ = listener.accept()
        transport = paramiko.Transport(connection)
        transports.append(transport)
        transport.add_server_key(key)
        transport.start_server(server=Server())
        channel = transport.accept(5)
        if channel is not None:
            stopped.wait(5)

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()

    def connect(route: SshRoute) -> paramiko.SSHClient:
        assert route == SshRoute(profile="science")
        client = paramiko.SSHClient()
        client.get_host_keys().add(f"[127.0.0.1]:{port}", "ssh-rsa", key)
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        client.connect(
            "127.0.0.1",
            port=port,
            username="test",
            password="test",
            allow_agent=False,
            look_for_keys=False,
            timeout=5,
        )
        return client

    monkeypatch.setattr(ssh_execution, "connect_route", connect)
    try:
        yield commands
    finally:
        stopped.set()
        for transport in transports:
            transport.close()
        listener.close()
        worker.join(6)
        assert not worker.is_alive()


@pytest.mark.parametrize("command,exit_code", [("inspect", 0), ("fail", 7)])
def test_real_ssh_streams_and_exit_status(
    ssh_peer: list[str], tmp_path: Path, command: str, exit_code: int
) -> None:
    result = ssh_execution.execute_ssh_command(
        SshRoute(profile="science"),
        command,
        root=tmp_path,
        session_id="sess_science",
        timeout_s=5,
        max_output_bytes=8192,
    )
    assert ssh_peer == [command]
    assert result["exit_code"] == exit_code
    assert result["stdout"] == "remote science\n"
    assert result["stderr"] == "instrument note\n"
    assert result["status"] == ("completed" if exit_code == 0 else "failed")
    presentation = ToolPresentation.model_validate(ssh_tools.ssh_presentation({}, result, None))
    assert presentation.status == ("completed" if exit_code == 0 else "failed")
    assert presentation.action == "Run SSH on science"
    assert presentation.blocks[0].type == "terminal"
    assert presentation.blocks[0].exit_code == exit_code
    assert "remote science" in presentation.blocks[0].text


def test_large_dual_streams_are_complete_on_disk(ssh_peer: list[str], tmp_path: Path) -> None:
    result = ssh_execution.execute_ssh_command(
        SshRoute(profile="science"),
        "large",
        root=tmp_path,
        session_id="sess_science",
        timeout_s=5,
        max_output_bytes=1024,
    )
    assert result["exit_code"] == 0
    assert len(json.dumps(result)) <= 12000
    for name, expected in (("stdout", b"science\n" * 10000), ("stderr", b"diagnostic\n" * 10000)):
        spill = result[f"{name}_spill"]
        path = Path(spill["path"])
        if not path.is_absolute():
            path = tmp_path / path
        assert path.read_bytes() == expected


@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_or_cancel_does_not_claim_remote_kill(
    ssh_peer: list[str], tmp_path: Path, cancel: bool
) -> None:
    start = time.monotonic()
    result = ssh_execution.execute_ssh_command(
        SshRoute(profile="science"),
        "wait",
        root=tmp_path,
        session_id="sess_science",
        timeout_s=0.2,
        max_output_bytes=1024,
        cancelled=(lambda: True) if cancel else None,
    )
    assert result["stop_reason"] == ("cancelled" if cancel else "timeout")
    assert result["exit_code"] is None and result["status"] == "failed"
    assert "Unknown" in result["remote_process_state"]
    assert time.monotonic() - start < 3


def test_exec_acknowledgement_has_a_real_deadline(ssh_peer: list[str], tmp_path: Path) -> None:
    start = time.monotonic()
    result = ssh_execution.execute_ssh_command(
        SshRoute(profile="science"),
        "no-ack",
        root=tmp_path,
        session_id="sess_science",
        timeout_s=0.2,
        max_output_bytes=1024,
    )
    assert ssh_peer == ["no-ack"]
    assert result["stop_reason"] == "timeout" and result["exit_code"] is None
    assert time.monotonic() - start < 3


def test_cancellation_checker_error_propagates_after_closing_channel(
    ssh_peer: list[str],
    tmp_path: Path,
) -> None:
    def broken_checker() -> bool:
        raise ValueError("cancellation state unavailable")

    with pytest.raises(ValueError, match="cancellation state unavailable"):
        ssh_execution.execute_ssh_command(
            SshRoute(profile="science"),
            "no-ack",
            root=tmp_path,
            session_id="sess_science",
            timeout_s=5,
            max_output_bytes=1024,
            cancelled=broken_checker,
        )


@pytest.mark.parametrize("mode,decision", [("ask", "deny"), ("ask", "allow"), ("bypass", "allow")])
def test_actual_approval_gate_precedes_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, decision: str
) -> None:
    from clio_agent.gact.app import _gact_app_context, _tool_session_context
    from tests.test_gact.task_records import record_assignment_fixture
    from tests.test_gact.test_a2ui_v3 import HEADERS, _session_client

    client, sid, _ = _session_client(tmp_path)
    calls: list[dict[str, Any]] = []

    def execute(route: SshRoute, command: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"route": route.model_dump(), "command": command})
        return {
            "command": command,
            "ssh_route": route.model_dump(),
            "status": "completed",
            "exit_code": 0,
            "stop_reason": "completed",
            "timeout_s": 60,
            "stdout": "",
            "stderr": "",
            "stdout_truncated": False,
            "stderr_truncated": False,
        }

    monkeypatch.setattr(ssh_tools, "execute_ssh_command", execute)
    with client:
        app = client.app
        assert isinstance(app, FastAPI)
        # SSH must traverse admission for a confirmed assignment before asking
        # permission or opening the connector, just like a production tool call.
        record_assignment_fixture(app, sid)
        assert (
            client.patch(
                f"/v1/sessions/{sid}", headers=HEADERS, json={"approval_mode": mode}
            ).status_code
            == 200
        )
        outcomes: list[Any] = []

        def run() -> None:
            with _gact_app_context(app), _tool_session_context(sid):
                token = ssh_tools.context.set_session_id(sid)
                try:
                    outcomes.append(
                        ssh_tools.ssh_execute(
                            "hostname",
                            ssh_route={
                                "host": "compute.example",
                                "jump_hosts": ["login"],
                                "user": "scientist",
                            },
                        )
                    )
                except PermissionError as exc:
                    outcomes.append(exc)
                finally:
                    ssh_tools.context.reset(token)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        if mode == "ask":
            # Wait for the prompt itself (or for the call to end without one): admission
            # and the permission request can take well over 5 s on a loaded CI runner.
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and not app.state.permissions and worker.is_alive():
                time.sleep(0.01)
            assert app.state.permissions, f"no permission prompt; call ended with {outcomes}"
            assert not calls
            pending = next(iter(app.state.permissions.values()))
            assert pending["tool_call"]["tool_name"] == "ssh_execute"
            assert "login → compute.example" in pending["summary"]
            assert pending["tool_call"]["input"]["command"] == "hostname"
            assert (
                client.post(
                    f"/v1/permissions/{pending['id']}", headers=HEADERS, json={"action": decision}
                ).status_code
                == 204
            )
        worker.join(5)
        assert not worker.is_alive()
        assert len(calls) == (0 if decision == "deny" else 1)
        assert isinstance(outcomes[0], PermissionError if decision == "deny" else dict)
        if mode == "bypass":
            row = next(iter(app.state.permissions.values()))
            assert row["status"] == "auto_approved" and row["reason"] == "approval_mode_bypass"


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"ssh_profile": "a", "ssh_route": {"host": "b"}},
        {"ssh_profile": "a", "timeout_s": float("nan")},
        {"ssh_profile": "a", "max_output_bytes": True},
    ],
)
def test_invalid_arguments_never_reach_connector(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        ssh_tools.ssh_execute("hostname", **kwargs)


def test_only_agents_whose_plugins_claim_ssh_receive_it() -> None:
    """A local shell does not imply remote execution: ssh comes only from its own claim."""
    for tools, expected in (
        ([], False),
        (["fs_read_file"], False),
        (["shell_bash"], False),
        (["shell_bash", "ssh_execute"], True),
    ):
        agent = SimpleNamespace(id="specialist", parent_id="main", tools=tools)
        native, gateway, withheld = build_native_tools(agent, tools, {})
        assert ("ssh_execute" in native) is expected
        assert "ssh_execute" not in gateway and withheld == {}


def test_ssh_is_declarable_without_enabling_local_shell() -> None:
    agent = SimpleNamespace(id="remote", tools=["ssh_execute"])
    native, gateway, withheld = build_native_tools(agent, ["ssh_execute"], {})
    assert list(native) == ["ssh_execute"] and gateway == [] and withheld == {}
    schema = native["ssh_execute"].format_as_litellm_function_call()["function"]["parameters"]
    assert schema["required"] == ["command"]
    route_schema = schema["properties"]["ssh_route"]
    assert {"type": "null"} in route_schema["anyOf"]
    route_object = next(branch for branch in route_schema["anyOf"] if branch["type"] == "object")
    assert "jump_hosts" in route_object["properties"]


def test_read_only_side_session_does_not_receive_ssh(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.gact.agents import session_tool_posture
    from clio_agent.gact.side_sessions import READ_ONLY_SIDE_PROFILE

    monkeypatch.setattr(
        session_tool_posture,
        "_active_session",
        lambda: SimpleNamespace(approval_profile=READ_ONLY_SIDE_PROFILE),
    )
    assert (
        session_tool_posture.restrict_tools_to_session_posture([ssh_tools.build_ssh_execute_tool()])
        == []
    )
