"""The run surface: options, attach-or-start errors, last-one-out, and runs in-process."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from clio_agent.client import (
    ClioClient,
    ClioOptions,
    ProviderNotConfigured,
    ServerStartFailed,
    ServerUnavailable,
)
from clio_agent.client import connection as conn

# --------------------------------------------------------------------------- #
# options
# --------------------------------------------------------------------------- #


def test_options_defaults_and_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLIO_GACT_BEARER_TOKEN", raising=False)
    options = ClioOptions(url="http://127.0.0.1:9", token=None)
    assert options.approval_mode == "bypass" and options.launch is True
    assert options.is_local and options.port == 9
    assert not ClioOptions(url="https://clio.example.org").is_local
    assert ClioOptions(url="https://clio.example.org").port == 443
    assert options.with_overrides(agent="factorio", model=None).agent == "factorio"
    for bad in (
        {"approval_mode": "yolo"},
        {"max_turns": 0},
        {"max_budget_usd": 0},
        {"model": "no-account"},
    ):
        with pytest.raises(ValueError):
            ClioOptions(**bad)


def test_token_comes_from_the_environment_for_a_remote_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIO_GACT_BEARER_TOKEN", "hand-set")
    assert ClioOptions(url="https://clio.example.org").resolved_token() == "hand-set"
    assert ClioOptions(url="https://clio.example.org", token="explicit").resolved_token() == (
        "explicit"
    )


# --------------------------------------------------------------------------- #
# attach or start: legible failures, nothing left behind
# --------------------------------------------------------------------------- #


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_remote_url_that_does_not_answer_raises_naming_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(conn, "_answers", lambda *_a: False)  # unit tests stay off the network
    options = ClioOptions(url="http://192.0.2.1:9", banner=False)  # TEST-NET: never answers
    with pytest.raises(ServerUnavailable, match=r"192\.0\.2\.1:9"):
        ClioClient(options).open()


def test_launch_false_with_nothing_running_raises_with_the_fix() -> None:
    options = ClioOptions(url=f"http://127.0.0.1:{_free_port()}", launch=False, banner=False)
    with pytest.raises(ServerUnavailable, match="launch=False"):
        with ClioClient(options):
            pass


def test_a_server_that_cannot_start_raises_with_its_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent import serve

    def _fail(*_a: Any, **_k: Any) -> str:
        raise serve.ServerStartTimeout({"reason": "spawn_timeout", "detail": "never healthy"})

    monkeypatch.setattr(serve, "ensure_server", _fail)
    options = ClioOptions(url=f"http://127.0.0.1:{_free_port()}", banner=False)
    with pytest.raises(ServerStartFailed, match="spawn_timeout"):
        ClioClient(options).open()


def _api(provider: dict[str, Any], seen: list[str]) -> httpx.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.url.path == "/v1/server/clients":
            return httpx.Response(201, json={"client_id": "client_1", "attached": 1})
        if request.url.path.startswith("/v1/server/clients/"):
            return httpx.Response(200, json={"attached": 0, "stopping": True})
        return httpx.Response(200, json=provider)

    return httpx.Client(base_url="http://127.0.0.1:9", transport=httpx.MockTransport(handle))


def test_no_provider_raises_and_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conn, "_answers", lambda *_a: True)
    seen: list[str] = []
    http = _api({"configured": False, "state": "idle", "error": "no provider"}, seen)
    with pytest.raises(ProviderNotConfigured, match="CLIO_LM_PROVIDER"):
        conn.open_connection(ClioOptions(url="http://127.0.0.1:9", banner=False), http)
    assert seen[-1] == "DELETE /v1/server/clients/client_1"  # nothing stays registered


def test_attached_server_is_never_stopped(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    monkeypatch.setattr(conn, "_answers", lambda *_a: True)
    import clio_agent.serve_exit as serve_exit

    monkeypatch.setattr(serve_exit, "await_server_exit", lambda *_a: pytest.fail("stopped"))
    seen: list[str] = []
    http = _api({"configured": True, "provider": "claude_code", "model": "m"}, seen)
    opened = conn.open_connection(ClioOptions(url="http://127.0.0.1:9"), http)
    assert opened.started is False and opened.client_id == "client_1"
    banner = capsys.readouterr().err
    assert "attached to the CLIO server at http://127.0.0.1:9" in banner
    assert "nothing will be asked" in banner and "Approval mode: bypass" in banner
    assert conn.close_connection(opened, http)["stopped"] is None


def test_started_server_is_waited_out_when_this_was_the_last_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import clio_agent.serve_exit as serve_exit

    waited: list[int] = []
    monkeypatch.setattr(serve_exit, "await_server_exit", lambda port: waited.append(port) or {})
    http = _api({}, [])
    started = conn.Connection("http://127.0.0.1:9", started=True, client_id="client_1", port=9)
    conn.close_connection(started, http)
    assert waited == [9]
