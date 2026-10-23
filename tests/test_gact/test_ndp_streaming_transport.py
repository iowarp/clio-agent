"""The isolated SDK execution boundary protects secrets and enforces finite captures."""

from __future__ import annotations

import json
import subprocess
from typing import Any
from unittest.mock import Mock

import pytest

from clio_agent.gact.storage.ndp_endpoint_models import EndpointConnection
from clio_agent.gact.storage.ndp_streaming_transport import run_sdk


@pytest.fixture
def connection() -> EndpointConnection:
    """Bind an explicitly configured local SDK runtime to an authenticated endpoint."""
    return EndpointConnection(
        id="endpoint",
        workspace_id="workspace",
        principal="owner",
        name="NDP",
        url="https://ndp.example",
        authentication="ndp",
        observed_at="now",
        schema_document={},
        streaming_runtime={"python": "/sdk/python", "profile_path": "/private/profile.json"},
    )


def test_credentials_use_private_stdin_only(
    connection: EndpointConnection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither the command line nor returned receipt exposes the NDP account token."""
    runner = Mock(
        return_value=subprocess.CompletedProcess([], 0, b'{"count": 0}', b"private diagnostics")
    )
    monkeypatch.setattr("clio_agent.gact.storage.ndp_streaming_transport.subprocess.run", runner)
    assert run_sdk(connection, {"action": "open"}, "private-account-token", 10) == {"count": 0}
    args, kwargs = runner.call_args
    assert "private-account-token" not in " ".join(args[0])
    payload = json.loads(kwargs["input"])
    assert payload["token"] == "private-account-token"
    assert payload["profile_path"] == "/private/profile.json"
    assert kwargs["timeout"] == 10
    with pytest.raises(PermissionError, match="Connect the NDP account"):
        run_sdk(connection, {}, None, 10)
    assert runner.call_count == 1


@pytest.mark.parametrize("outcome", [124, "timeout"])
def test_local_deadline_is_a_timeout_without_private_diagnostics(
    connection: EndpointConnection,
    monkeypatch: pytest.MonkeyPatch,
    outcome: Any,
) -> None:
    """The remote watchdog and parent process timeout have the same public semantics."""
    runner = Mock(
        return_value=subprocess.CompletedProcess([], outcome, b"", b"private diagnostics")
    )
    if outcome == "timeout":
        runner.side_effect = subprocess.TimeoutExpired("sdk", 10, stderr=b"private diagnostics")
    monkeypatch.setattr("clio_agent.gact.storage.ndp_streaming_transport.subprocess.run", runner)
    with pytest.raises(TimeoutError, match="finite capture budget") as error:
        run_sdk(connection, {}, "private-account-token", 10)
    assert "private diagnostics" not in str(error.value)


def test_ssh_exit_status_does_not_truncate_late_stdout(
    connection: EndpointConnection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful remote exit can precede the final JSON packet and its EOF."""

    class Channel:
        eof_received = False

        def __init__(self) -> None:
            self.checks = 0
            self.chunks = [b'{"count":', b" 1000}"]

        def recv_ready(self) -> bool:
            self.checks += 1
            return bool(self.chunks) and self.checks >= 3

        def recv(self, size: int) -> bytes:
            result = self.chunks.pop(0)
            self.eof_received = not self.chunks
            return result

        def recv_stderr_ready(self) -> bool:
            return False

        def exit_status_ready(self) -> bool:
            return True

        def recv_exit_status(self) -> int:
            return 0

        def shutdown_write(self) -> None:
            pass

        def close(self) -> None:
            pass

    channel = Channel()
    client = Mock()
    client.exec_command.return_value = (Mock(), Mock(channel=channel), Mock())
    monkeypatch.setattr(
        "clio_agent.gact.storage.ndp_streaming_transport.connect_profile", Mock(return_value=client)
    )
    connection.ssh_profile = "ares"
    assert run_sdk(connection, {}, "private-account-token", 5) == {"count": 1000}
    assert not channel.chunks
    client.close.assert_called_once()
