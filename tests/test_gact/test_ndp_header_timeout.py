"""Diagnose an SSH HTTP header timeout and release all transport owners."""

from unittest.mock import MagicMock

import httpx
import pytest

from clio_agent.gact.storage import ndp_endpoint_transport


def test_header_timeout_names_budget_and_closes_ssh(monkeypatch: pytest.MonkeyPatch) -> None:
    client, connection, channel = MagicMock(), MagicMock(), MagicMock()
    client.get_transport.return_value.open_channel.return_value = channel
    connection.getresponse.side_effect = TimeoutError()
    monkeypatch.setattr(ndp_endpoint_transport, "connect_profile", lambda _: client)
    monkeypatch.setattr(ndp_endpoint_transport.http.client, "HTTPConnection", lambda *a: connection)
    transport = ndp_endpoint_transport.EndpointSSHTransport("ares", 10)
    with pytest.raises(TimeoutError, match="response headers within 10 seconds"):
        transport.handle_request(httpx.Request("GET", "http://127.0.0.1:8002/streams"))
    channel.close.assert_called_once()
    connection.close.assert_called_once()
    client.close.assert_called_once()
