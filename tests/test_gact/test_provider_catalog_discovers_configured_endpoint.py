"""#1418 cause C contract test: discovery must probe the CONFIGURED endpoint.

Before the fix, ``provider_catalog_snapshot.discover_provider`` always probed
the catalog preset's STATIC default ``api_base`` (``http://127.0.0.1:8088/v1``
for llama.cpp), never the URL a person actually configured. A llama.cpp
server running on a non-default port was therefore never really asked, its
real model id was never discovered, and the catalog kept serving nothing
useful for it.

This starts a REAL local HTTP server on an ephemeral (non-default) port that
answers ``/v1/models`` the way llama.cpp does, binds it as the active global
LM by priming ``app.state.lm_config`` directly (the same shape ``PUT
/v1/providers/lm`` writes), and asserts ``GET /v1/provider-catalog`` lists
``llama_cpp`` with the model that server actually reported.

Hermetic: the catalog read also discovers every other provider and consults the
external model catalogs (models.dev, LiteLLM's community map). The
``recorded_remotes`` fixture points all of them at a loopback server that replays
recorded responses, so the test never leaves this machine (the suite's network
guard, tests/_network_guard.py, fails it if it does).
"""

from __future__ import annotations

import dataclasses
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app

DISCOVERED_MODEL_ID = "qwen3-4b-instruct-2507-gguf"


class _FakeLlamaCppHandler(BaseHTTPRequestHandler):
    """Answers ``GET /v1/models`` the way an OpenAI-compatible llama.cpp server does."""

    requests: list[str] = []  # noqa: RUF012 - per-test subclass, reset each test

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler method name
        self.requests.append(self.path)
        if self.path == "/v1/models":
            body = json.dumps({"data": [{"id": DISCOVERED_MODEL_ID, "object": "model"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        pass


@pytest.fixture()
def fake_llama_cpp_server():
    _FakeLlamaCppHandler.requests = []
    with ThreadingHTTPServer(("127.0.0.1", 0), _FakeLlamaCppHandler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield server
        finally:
            server.shutdown()
            worker.join(timeout=5)


# Recorded responses, trimmed to the shape each consumer parses: an OpenAI-style model
# list for every remote provider's ``/models``, and the two external catalogs (an empty
# document is a valid, fact-free catalog for both).
_RECORDED_MODELS = {"object": "list", "data": [{"id": "recorded-model", "object": "model"}]}
_RECORDED_CATALOG: dict[str, Any] = {}


class _RecordedRemotesHandler(BaseHTTPRequestHandler):
    """Replays the recorded remote responses; any other path is a plain 404."""

    requests: list[str] = []  # noqa: RUF012 - reset per test by the fixture

    def _reply(self) -> None:
        self.requests.append(self.path)
        path = self.path.split("?", 1)[0]
        if path.endswith("/models"):
            payload: Any = _RECORDED_MODELS
        elif path.endswith(("/models.json", "/cost-map.json")):
            payload = _RECORDED_CATALOG
        else:
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _reply  # noqa: N815 - stdlib handler method name
    do_POST = _reply  # noqa: N815 - stdlib handler method name

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        pass


def _is_remote(api_base: str) -> bool:
    parts = urlsplit(api_base)
    return parts.scheme in ("http", "https") and parts.hostname not in ("127.0.0.1", "localhost")


@pytest.fixture()
def recorded_remotes(monkeypatch: pytest.MonkeyPatch):
    """Point every remote endpoint the catalog read touches at a loopback replay server."""
    import litellm

    from clio_agent.providers import catalog
    from clio_agent.providers.handshake.sources import litellm_catalog, models_dev

    _RecordedRemotesHandler.requests = []
    with ThreadingHTTPServer(("127.0.0.1", 0), _RecordedRemotesHandler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{server.server_port}"
        monkeypatch.setattr(
            catalog,
            "PROVIDERS",
            tuple(
                dataclasses.replace(p, api_base=f"{base}/{p.id}/v1")
                if _is_remote(p.api_base)
                else p
                for p in catalog.PROVIDERS
            ),
        )
        monkeypatch.setattr(models_dev, "MODELS_DEV_URL", f"{base}/models-dev/models.json")
        monkeypatch.setattr(litellm, "model_cost_map_url", f"{base}/litellm/cost-map.json")
        # The AWS credential chain (Bedrock readiness) otherwise asks the EC2 instance
        # metadata endpoint, 169.254.169.254.
        monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
        models_dev._catalog.cache_clear()
        litellm_catalog._catalog.cache_clear()
        try:
            yield _RecordedRemotesHandler
        finally:
            models_dev._catalog.cache_clear()
            litellm_catalog._catalog.cache_clear()
            server.shutdown()
            worker.join(timeout=5)


def test_catalog_lists_llama_cpp_with_the_discovered_model_at_a_nondefault_port(
    tmp_path: Path,
    fake_llama_cpp_server: ThreadingHTTPServer,
    recorded_remotes: type[_RecordedRemotesHandler],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = fake_llama_cpp_server.server_port
    assert port != 8088  # the catalog's static default port -- proves this is a REAL probe

    # The snapshot catalog's connectivity gate (openai_compat.py::_requires_key)
    # keys off provider_kind, and llama.cpp shares the "openai" kind with real
    # cloud providers -- so it is gated behind a resolvable key exactly like
    # them (CLIO_LM_API_KEY is the generic fallback every kind honors). That
    # api-KEY gate is a separate, pre-existing quirk from the api-BASE bug
    # this test targets (#1418 cause C), so it is satisfied here rather than
    # widened as a side effect of this fix.
    monkeypatch.setenv("CLIO_LM_API_KEY", "llama-cpp")

    app = build_app(sessions_path=tmp_path / "sessions.json")
    # The shape PUT /v1/providers/lm writes to app.state.lm_config on a real
    # bind (gact/routes/providers.py); primed directly so this test exercises
    # discovery without the unrelated cost of full agent construction.
    app.state.lm_config = {
        "provider_id": "llama_cpp",
        "provider": "openai",
        "api_base": f"http://127.0.0.1:{port}/v1",
        "model": DISCOVERED_MODEL_ID,
    }

    with TestClient(app) as client:
        response = client.get("/v1/provider-catalog", params={"refresh": "true"})

    assert response.status_code == 200
    body = response.json()
    llama_cpp = next(p for p in body["providers"] if p["id"] == "llama_cpp")
    assert llama_cpp["endpoint"] == f"http://127.0.0.1:{port}/v1"
    model_ids = {m["model_id"] for m in llama_cpp["models"]}
    assert DISCOVERED_MODEL_ID in model_ids
    assert "/v1/models" in _FakeLlamaCppHandler.requests
    # The rest of the catalog was discovered too -- against the recorded remotes.
    assert any(path.endswith("/models") for path in recorded_remotes.requests)
