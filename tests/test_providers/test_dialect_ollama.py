"""Contract tests for the Ollama dialect adapter, against RECORDED responses.

Fixtures: the existing ``tests/test_providers/fixtures/handshake/ollama_api_show_qwen3.json``
(P4a's) plus the new ``tests/fixtures/capabilities/ollama/`` (``/api/ps``,
``/api/version``). Covers the DEPLOYMENT facts P4a's handshake does not
already cover (brief Part 6): the Modelfile ``parameters`` blob, the loaded
context from ``/api/ps`` (``ollama-probe-context``), and the endpoint
fingerprint from ``/api/version``.

``fetch_deployment_facts`` is exercised against the REAL ``ollama.AsyncClient``
(brief: "through the official ollama Python client") wired to an
``httpx.MockTransport`` -- a fake HTTP server with no real socket -- serving
the same recorded JSON, so the client's own request/response wire contract is
tested too, not just this module's dict-shaped parsing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import ollama
import pytest

from clio_agent.providers.capabilities.dialects import ollama as ollama_dialect

HANDSHAKE_FIXTURES = Path(__file__).parent / "fixtures" / "handshake"
CAPABILITY_FIXTURES = Path(__file__).parent.parent / "fixtures" / "capabilities" / "ollama"


def _load(directory: Path, name: str) -> Any:
    return json.loads((directory / name).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- pure parsing


def test_parse_modelfile_parameters_reads_num_ctx_and_sampling() -> None:
    show = _load(HANDSHAKE_FIXTURES, "ollama_api_show_qwen3.json")

    params = ollama_dialect.parse_modelfile_parameters(show["parameters"])

    assert params == {"num_ctx": 40960, "temperature": 0.6, "top_p": 0.95, "top_k": 20}


def test_parse_modelfile_parameters_collects_repeated_keys() -> None:
    params = ollama_dialect.parse_modelfile_parameters('stop "<|im_end|>"\nstop "<|end|>"\nnum_ctx 8192')

    assert params["stop"] == ["<|im_end|>", "<|end|>"]
    assert params["num_ctx"] == 8192


def test_parse_modelfile_parameters_empty_is_empty_dict() -> None:
    assert ollama_dialect.parse_modelfile_parameters(None) == {}
    assert ollama_dialect.parse_modelfile_parameters("") == {}


def test_loaded_context_from_ps_matches_by_model_id() -> None:
    ps = _load(CAPABILITY_FIXTURES, "api_ps.json")

    assert ollama_dialect.loaded_context_from_ps(ps, "qwen3:8b") == 8192
    assert ollama_dialect.loaded_context_from_ps(ps, "other:model") is None


def test_digest_from_ps_matches_by_model_id() -> None:
    ps = _load(CAPABILITY_FIXTURES, "api_ps.json")

    assert ollama_dialect.digest_from_ps(ps, "qwen3:8b") == "sha256:abc123def456"
    assert ollama_dialect.digest_from_ps(ps, "other:model") is None


def test_build_deployment_extra_uses_smaller_of_num_ctx_and_loaded_context() -> None:
    show = _load(HANDSHAKE_FIXTURES, "ollama_api_show_qwen3.json")
    ps = _load(CAPABILITY_FIXTURES, "api_ps.json")

    deployment = ollama_dialect.build_deployment_extra(
        provider_id="ollama",
        api_base="http://127.0.0.1:11434",
        model_id="qwen3:8b",
        show_parameters=show["parameters"],
        ps_payload=ps,
    )

    # Modelfile num_ctx=40960, but only 8192 is actually loaded right now.
    assert deployment.context_served.value == 8192
    assert deployment.fingerprint == "ollama:digest=sha256:abc123def456:loaded_context=8192"


def test_build_deployment_extra_uses_whichever_side_is_known() -> None:
    only_modelfile = ollama_dialect.build_deployment_extra(
        provider_id="ollama",
        api_base="http://127.0.0.1:11434",
        model_id="qwen3:8b",
        show_parameters="num_ctx 40960",
        ps_payload=None,
    )
    assert only_modelfile.context_served.value == 40960

    only_ps = ollama_dialect.build_deployment_extra(
        provider_id="ollama",
        api_base="http://127.0.0.1:11434",
        model_id="qwen3:8b",
        show_parameters=None,
        ps_payload=_load(CAPABILITY_FIXTURES, "api_ps.json"),
    )
    assert only_ps.context_served.value == 8192


def test_fingerprint_from_version() -> None:
    payload = _load(CAPABILITY_FIXTURES, "api_version.json")

    assert ollama_dialect.fingerprint_from_version(payload["version"]) == "ollama:version=0.5.4"
    assert ollama_dialect.fingerprint_from_version(None) == ""


def test_build_endpoint_capabilities_reports_version_and_fingerprint() -> None:
    endpoint = ollama_dialect.build_endpoint_capabilities(
        "ollama", "http://127.0.0.1:11434", "qwen3:8b", version="0.5.4"
    )

    assert endpoint.dialect == "ollama"
    assert endpoint.fingerprint == "ollama:version=0.5.4"
    assert endpoint.server_version.value == "0.5.4"


# --------------------------------------------------------------------------- real client, fake transport


def _mock_transport(*, show_payload: Any, ps_payload: Any) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/show":
            return httpx.Response(200, json=show_payload)
        if request.url.path == "/api/ps":
            return httpx.Response(200, json=ps_payload)
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_fetch_deployment_facts_through_the_real_ollama_client() -> None:
    show_payload = _load(HANDSHAKE_FIXTURES, "ollama_api_show_qwen3.json")
    ps_payload = _load(CAPABILITY_FIXTURES, "api_ps.json")
    client = ollama.AsyncClient(
        host="http://127.0.0.1:11434",
        transport=_mock_transport(show_payload=show_payload, ps_payload=ps_payload),
    )

    deployment = await ollama_dialect.fetch_deployment_facts(
        client, provider_id="ollama", api_base="http://127.0.0.1:11434", model_id="qwen3:8b"
    )

    assert deployment.context_served.value == 8192
    assert deployment.fingerprint == "ollama:digest=sha256:abc123def456:loaded_context=8192"


@pytest.mark.asyncio
async def test_fetch_deployment_facts_survives_a_missing_ps_row() -> None:
    show_payload = _load(HANDSHAKE_FIXTURES, "ollama_api_show_qwen3.json")
    client = ollama.AsyncClient(
        host="http://127.0.0.1:11434",
        transport=_mock_transport(show_payload=show_payload, ps_payload={"models": []}),
    )

    deployment = await ollama_dialect.fetch_deployment_facts(
        client, provider_id="ollama", api_base="http://127.0.0.1:11434", model_id="qwen3:8b"
    )

    # falls back to the Modelfile's own num_ctx when nothing is currently loaded
    assert deployment.context_served.value == 40960


@pytest.mark.asyncio
async def test_fetch_version_reads_the_native_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/version"
        return httpx.Response(200, json={"version": "0.5.4"})

    client = ollama.AsyncClient(host="http://127.0.0.1:11434", transport=httpx.MockTransport(handler))

    version = await ollama_dialect.fetch_version(client, "http://127.0.0.1:11434")

    assert version == "0.5.4"


@pytest.mark.asyncio
async def test_fetch_version_is_best_effort_on_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client = ollama.AsyncClient(host="http://127.0.0.1:11434", transport=httpx.MockTransport(handler))

    version = await ollama_dialect.fetch_version(client, "http://127.0.0.1:11434")

    assert version is None
