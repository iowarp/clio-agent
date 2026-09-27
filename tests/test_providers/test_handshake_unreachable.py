"""A server that is down is named, with its address, in plain words.

Live (2026-09-27): with LM Studio, Ollama, llama.cpp and vLLM all down, each
one's reason was the same raw ``ConnectError: All connection attempts failed``.
Nothing said which server or where, so in the picker the sentence read as one
provider's error following the selection around. These tests probe a real
closed port (a real ``httpx.ConnectError``) through the real handshakes.
"""

from __future__ import annotations

import socket

import httpx
import pytest

from clio_agent.providers.catalog import get_provider
from clio_agent.providers.handshake import get_handshake_for
from clio_agent.providers.handshake.base import HandshakeContext
from clio_agent.providers.handshake.model import ConnectivityState
from clio_agent.providers.handshake.unreachable import (
    SERVER_NOT_ANSWERING,
    SERVER_NOT_RUNNING,
    server_address,
    unreachable_reason,
)


def _closed_port() -> int:
    """A loopback port nothing listens on (bound, then released)."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.parametrize(
    ("provider_id", "path", "label"),
    [
        ("lm_studio", "/v1", "LM Studio"),
        ("ollama", "", "Ollama"),
        ("llama_cpp", "/v1", "llama.cpp server"),
        ("vllm", "/v1", "vLLM"),
    ],
)
async def test_a_local_server_that_is_down_says_which_one_and_where(
    provider_id: str, path: str, label: str
) -> None:
    provider = get_provider(provider_id)
    assert provider is not None
    handshake = get_handshake_for(provider.provider_kind, provider)
    port = _closed_port()
    ctx = HandshakeContext(
        provider_id=provider_id,
        provider_kind=provider.provider_kind,
        api_base=f"http://127.0.0.1:{port}{path}",
        # A local server takes any key; the live path sends a placeholder.
        api_key="local",
    )

    async with httpx.AsyncClient(timeout=5.0) as client:
        result = await handshake.check_connectivity(client, ctx)

    assert result.connectivity is ConnectivityState.UNREACHABLE
    assert result.error_code == SERVER_NOT_RUNNING
    assert result.error == f"server_not_running: {label} isn't running at 127.0.0.1:{port}."
    assert "ConnectError" not in (result.error or "")


def test_a_saved_server_with_no_registry_row_is_named_by_its_id() -> None:
    code, reason = unreachable_reason(
        "my-gpu-box", "http://10.0.0.5:8000/v1", httpx.ConnectError("refused")
    )

    assert code == SERVER_NOT_RUNNING
    assert reason == "server_not_running: my-gpu-box isn't running at 10.0.0.5:8000."


def test_a_hosted_api_that_cannot_be_reached_is_a_connection_problem_not_a_server_to_start() -> (
    None
):
    code, reason = unreachable_reason(
        "openai", "https://api.openai.com/v1", httpx.ConnectError("name resolution failed")
    )

    assert code == SERVER_NOT_RUNNING
    assert "isn't running" not in reason
    assert reason == (
        "server_not_running: Couldn't connect to OpenAI API at api.openai.com. "
        "Check this computer's network connection."
    )


def test_a_server_that_times_out_says_it_did_not_answer() -> None:
    code, reason = unreachable_reason(
        "ollama", "http://127.0.0.1:11434", httpx.ReadTimeout("timed out")
    )

    assert code == SERVER_NOT_ANSWERING
    assert reason == "server_not_answering: Ollama at 127.0.0.1:11434 didn't answer in time."


def test_server_address_is_host_and_port() -> None:
    assert server_address("http://127.0.0.1:1234/v1") == "127.0.0.1:1234"
    assert server_address("not a url") == "not a url"
