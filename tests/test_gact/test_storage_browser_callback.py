"""Exercise receiver lifecycle with actual loopback sockets and isolated auth state."""

from __future__ import annotations

import socket
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from clio_agent.gact.storage.auth import StorageAuth
from clio_agent.gact.storage.oauth_return import StorageOAuthReturn


def test_unused_callback_expires_and_releases_listener() -> None:
    receiver = StorageOAuthReturn("state", time.time() + 0.1)
    receiver.thread.join(timeout=2)
    assert not receiver.thread.is_alive()
    # Rebinding proves release without depending on Windows' refused-connect delay.
    with socket.socket() as replacement:
        replacement.bind(("127.0.0.1", receiver.server.server_port))
    receiver.close()


@pytest.mark.parametrize("action", ["denied", "restart", "disconnect", "sign_out"])
def test_callback_cancellation_cleans_up(tmp_path: Path, action: str) -> None:
    auth = StorageAuth(tmp_path / "credentials.json")
    record = auth.account_login("alice", "clio", "google_drive")
    flow = auth.start(record, local_browser=True)
    query = parse_qs(urlparse(flow["authorization_url"]).query)
    receiver = auth._pending[flow["flow_id"]].receiver
    assert receiver is not None
    if action == "denied":
        response = httpx.get(
            receiver.redirect_uri,
            params={"state": query["state"][0], "error": "access_denied<script>"},
            trust_env=False,
        )
        assert "<script>" not in response.text
        with pytest.raises(ValueError, match="not approved"):
            auth.complete(record, flow["flow_id"], "")
    elif action == "restart":
        second = auth.start(record, local_browser=True)
        assert second["flow_id"] != flow["flow_id"]
        auth.disconnect(record.source.id)
    elif action == "disconnect":
        auth.disconnect(record.source.id)
    else:
        auth.sign_out("alice", "clio", "google_drive")
    assert not receiver.thread.is_alive()
    assert receiver.result() == ""
    assert flow["flow_id"] not in auth._pending
    assert not auth.connected(record)
