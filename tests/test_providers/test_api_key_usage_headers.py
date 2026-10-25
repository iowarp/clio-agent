"""An Anthropic or OpenAI API key's successful calls record its rate-limit windows (M-D12)."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from clio_agent.providers.usage.api_key_headers import api_key_snapshot
from clio_agent.providers.usage.snapshot import USAGE_BOARD

_ANTHROPIC_HEADERS = {
    "anthropic-ratelimit-requests-limit": "50",
    "anthropic-ratelimit-requests-remaining": "45",
    "anthropic-ratelimit-requests-reset": "2026-10-25T12:00:30Z",
    "anthropic-ratelimit-input-tokens-limit": "20000",
    "anthropic-ratelimit-input-tokens-remaining": "5000",
    "anthropic-ratelimit-input-tokens-reset": "2026-10-25T12:00:10Z",
}
_OPENAI_HEADERS = {
    "x-ratelimit-limit-requests": "500",
    "x-ratelimit-remaining-requests": "499",
    "x-ratelimit-reset-requests": "120ms",
    "x-ratelimit-limit-tokens": "30000",
    "x-ratelimit-remaining-tokens": "27000",
    "x-ratelimit-reset-tokens": "6m0s",
}


def test_anthropic_headers_become_windows_with_their_reset_times() -> None:
    snapshot = api_key_snapshot("anthropic", _ANTHROPIC_HEADERS)

    assert snapshot is not None and snapshot.source == "anthropic_rate_limit_headers"
    assert [(w.name, w.used_percent, w.resets_at) for w in snapshot.windows] == [
        ("Requests", 10.0, "2026-10-25T12:00:30+00:00"),
        ("Input tokens", 75.0, "2026-10-25T12:00:10+00:00"),
    ]


def test_openai_headers_become_windows_reset_after_their_durations() -> None:
    snapshot = api_key_snapshot("openai", _OPENAI_HEADERS)

    assert snapshot is not None and snapshot.source == "openai_rate_limit_headers"
    assert [(w.name, w.used_percent) for w in snapshot.windows] == [
        ("Requests", 0.2),
        ("Tokens", 10.0),
    ]
    assert all(w.resets_at for w in snapshot.windows)


def test_a_response_without_rate_limit_headers_records_nothing() -> None:
    assert api_key_snapshot("openai", {"content-type": "application/json"}) is None
    assert api_key_snapshot("openai", {"x-ratelimit-limit-requests": "500"}) is None


class _Provider(BaseHTTPRequestHandler):
    """Answers the Anthropic Messages and OpenAI chat routes with rate-limit headers."""

    def do_POST(self) -> None:  # noqa: N802 - http.server's name
        self.rfile.read(int(self.headers.get("content-length") or 0))
        if self.path.endswith("/messages"):
            headers, body = (
                _ANTHROPIC_HEADERS,
                {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-test",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 3, "output_tokens": 1},
                },
            )
        else:
            headers, body = (
                _OPENAI_HEADERS,
                {
                    "id": "chat_1",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "gpt-test",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
                },
            )
        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:
        del args


@pytest.fixture
def provider_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    ("provider", "model", "api_base_suffix", "first_window"),
    [
        ("anthropic", "claude-test", "/v1", "Requests"),
        ("openai", "gpt-test", "/v1", "Requests"),
    ],
)
def test_a_successful_api_key_call_records_the_account_windows(
    provider: str, model: str, api_base_suffix: str, first_window: str, provider_url: str
) -> None:
    from clio_agent.config import LMProviderConfig
    from clio_agent.lm.factory import create_lm

    lm = create_lm(
        LMProviderConfig(
            provider=provider,
            provider_id=provider,
            model=model,
            api_key="test-key",
            api_base=provider_url + api_base_suffix,
        )
    )
    USAGE_BOARD._latest.pop(provider, None)

    assert lm("hello")
    synced = USAGE_BOARD.latest(provider)
    assert synced is not None and synced.windows[0].name == first_window

    USAGE_BOARD._latest.pop(provider, None)
    assert asyncio.run(lm.acall("hello"))
    awaited = USAGE_BOARD.latest(provider)
    assert awaited is not None and awaited.windows[0].name == first_window
