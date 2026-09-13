"""Regressions for uncapped local-model requests (#1323)."""

import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from litellm import ModelResponse

from clio_agent.config import LMProviderConfig, create_lm, create_planner_lm
from clio_agent.providers.handshake.model import (
    AuthState,
    ConnectivityState,
    HandshakeReport,
    ModelProfile,
)


@pytest.mark.parametrize("provider", ["vllm", "lm_studio", "argonne", "openai"])
def test_zero_survives_config_copy_and_handshake(provider: str) -> None:
    config = LMProviderConfig(provider=provider, model="granite-4.2-30b", api_key="test")
    config.apply_handshake(
        HandshakeReport(
            provider_id=provider,
            provider_kind=config.provider,
            connectivity=ConnectivityState.OK,
            auth=AuthState.OK,
            models=(ModelProfile(id=config.model, context_window=131072, output_limit=8192),),
        )
    )
    assert config.max_tokens == config.planner_max_tokens == 0
    assert replace(config, temperature=0.5).max_tokens == 0


@pytest.mark.parametrize("planner", [False, True])
@pytest.mark.parametrize("cap", [0, 1234])
def test_output_cap_reaches_provider_only_when_explicit(planner: bool, cap: int) -> None:
    config = LMProviderConfig(
        provider="vllm", model="granite-4.2-30b", max_tokens=cap, planner_max_tokens=cap
    )
    lm = (create_planner_lm if planner else create_lm)(config)
    if cap:
        assert lm.kwargs["max_tokens"] == cap
    else:
        assert "max_tokens" not in lm.kwargs
        assert "max_completion_tokens" not in lm.kwargs


def test_explicit_zero_planner_cap_differs_from_absent_planner_cap() -> None:
    omitted = LMProviderConfig(
        provider="vllm", model="granite", max_tokens=1234, planner_max_tokens=0
    )
    inherited = LMProviderConfig(provider="vllm", model="granite", max_tokens=1234)
    omitted_lm = create_planner_lm(omitted)
    inherited_lm = create_planner_lm(inherited)
    assert "max_tokens" not in omitted_lm.kwargs
    assert inherited_lm.kwargs["max_tokens"] == 1234
    assert omitted_lm._clio_provider_config.max_tokens == 0
    assert inherited_lm._clio_provider_config.max_tokens == 1234


def test_length_finish_is_an_explicit_failure() -> None:
    from clio_agent.lm.io_logging import LMOutputTruncatedError

    lm = create_lm(LMProviderConfig(provider="vllm", model="granite-4.2-30b"))
    response = ModelResponse(
        choices=[
            {"message": {"role": "assistant", "content": "partial"}, "finish_reason": "length"}
        ]
    )
    with pytest.raises(LMOutputTruncatedError, match="truncated") as raised:
        lm._process_lm_response(response, None, [{"role": "user", "content": "report"}])
    assert lm.history[-1]["response"] is response
    assert raised.value.to_dict()["details"]["reason"] == "output_truncated"


def test_unknown_provider_cannot_inherit_lm_studio_defaults() -> None:
    with pytest.raises(ValueError, match="Unknown LM provider"):
        LMProviderConfig(provider="misspelled-vllm", api_base="http://localhost:8000/v1")


def test_unresolved_vllm_model_fails_before_transport() -> None:
    with pytest.raises(ValueError, match="No model configured"):
        create_lm(LMProviderConfig(provider="vllm", model=""))


@pytest.mark.parametrize("field", ["max_tokens", "planner_max_tokens"])
def test_negative_output_caps_are_invalid(field: str) -> None:
    with pytest.raises(ValueError, match="must be non-negative"):
        LMProviderConfig(provider="vllm", model="granite", **{field: -1})


@pytest.mark.parametrize("cap", [0, 1234])
def test_real_openai_compatible_wire_omits_default_cap(
    cap: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            body = json.dumps(
                {
                    "id": "completion-test",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "granite-4.2-30b",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "done"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    monkeypatch.setattr("clio_agent.lm.io_logging._token_liveness_enabled", lambda: False)
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            lm = create_lm(
                LMProviderConfig(
                    provider="vllm",
                    model="granite-4.2-30b",
                    api_key="test",
                    api_base=f"http://127.0.0.1:{server.server_port}/v1",
                    max_tokens=cap,
                )
            )
            lm(messages=[{"role": "user", "content": "report"}])
        finally:
            server.shutdown()
            worker.join(timeout=5)
    assert len(requests) == 1
    assert requests[0]["model"] == "granite-4.2-30b"
    if cap:
        assert requests[0]["max_tokens"] == cap
    else:
        assert "max_tokens" not in requests[0]
        assert "max_completion_tokens" not in requests[0]
