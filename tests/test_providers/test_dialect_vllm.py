"""Contract tests for the vLLM dialect adapter, against RECORDED responses.

Fixtures: ``tests/fixtures/capabilities/vllm/`` (model-capabilities brief Part 6
/ 9.2). Covers ``/v1/models`` (``max_model_len`` -> served context, ``root`` ->
the Hugging Face repo link) and ``GET /version`` (endpoint fingerprint).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from clio_agent.providers.capabilities.dialects import vllm

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "capabilities" / "vllm"


def _load(name: str) -> Any:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def test_find_model_row_matches_by_id() -> None:
    payload = _load("v1_models.json")

    row = vllm.find_model_row(payload, "Qwen/Qwen3-8B")

    assert row is not None
    assert row["max_model_len"] == 40960


def test_find_model_row_falls_back_to_sole_row_when_id_unmatched() -> None:
    payload = _load("v1_models.json")

    row = vllm.find_model_row(payload, "local-model")

    assert row is not None
    assert row["root"] == "Qwen/Qwen3-8B"


def test_parse_models_row_reports_served_context_and_root_link() -> None:
    payload = _load("v1_models.json")
    row = vllm.find_model_row(payload, "Qwen/Qwen3-8B")
    assert row is not None

    deployment = vllm.parse_models_row(row, provider_id="vllm", api_base="http://127.0.0.1:8000/v1")

    assert deployment.context_served.value == 40960
    assert deployment.context_served.source == "server_report"
    assert deployment.model_key.value == "Qwen/Qwen3-8B"
    assert deployment.model_key.detail.startswith("link rule=hf_vllm_root")


def test_deployment_fingerprint_changes_with_root_or_context() -> None:
    a = vllm.deployment_fingerprint_from_row({"root": "Qwen/Qwen3-8B", "max_model_len": 40960})
    b = vllm.deployment_fingerprint_from_row({"root": "Qwen/Qwen3-8B", "max_model_len": 8192})
    c = vllm.deployment_fingerprint_from_row({"root": "Qwen/Qwen3-14B", "max_model_len": 40960})

    assert len({a, b, c}) == 3
    assert vllm.deployment_fingerprint_from_row({}) == ""


def test_fingerprint_from_version() -> None:
    payload = _load("version.json")

    fp = vllm.fingerprint_from_version(payload["version"])

    assert fp == "vllm:version=0.11.0"
    assert vllm.fingerprint_from_version(None) == ""


def test_build_endpoint_capabilities_uses_hosted_vllm_litellm_provider() -> None:
    endpoint = vllm.build_endpoint_capabilities(
        "vllm", "http://127.0.0.1:8000/v1", "Qwen/Qwen3-8B", version="0.11.0"
    )

    assert endpoint.dialect == "vllm"
    assert endpoint.fingerprint == "vllm:version=0.11.0"
    assert endpoint.server_version.value == "0.11.0"
    assert endpoint.accepted_params.known
    assert "top_k" in (endpoint.accepted_params.value or set())


def test_build_model_capabilities_is_a_bare_stub_for_the_hf_layer_to_fill() -> None:
    model = vllm.build_model_capabilities("Qwen/Qwen3-8B", {"root": "Qwen/Qwen3-8B"})

    assert model.model_key == "Qwen/Qwen3-8B"
    assert not model.context_max.known


# --------------------------------------------------------------------------- fetch (fake HTTP client)


@dataclass
class _FakeResponse:
    status_code: int
    _payload: Any = None

    def json(self) -> Any:
        return self._payload


class _FakeClient:
    def __init__(self, *, routes: dict[str, Any] | None = None, fail: bool = False) -> None:
        self._routes = routes or {}
        self._fail = fail
        self.requested: list[str] = []

    async def get(self, url: str, **_: object) -> _FakeResponse:
        self.requested.append(url)
        if self._fail:
            raise ConnectionError("unreachable")
        if url in self._routes:
            return _FakeResponse(200, self._routes[url])
        return _FakeResponse(404)


@pytest.mark.asyncio
async def test_fetch_models_normalizes_a_bare_root_and_a_v1_suffixed_api_base() -> None:
    payload = _load("v1_models.json")

    # a configured provider's api_base already ends in /v1...
    client_a = _FakeClient(routes={"http://127.0.0.1:8000/v1/models": payload})
    assert await vllm.fetch_models(client_a, "http://127.0.0.1:8000/v1") == payload

    # ...and a bare host root (ALCF's per-job endpoint) hits the SAME path.
    client_b = _FakeClient(routes={"http://127.0.0.1:8000/v1/models": payload})
    assert await vllm.fetch_models(client_b, "http://127.0.0.1:8000") == payload


@pytest.mark.asyncio
async def test_fetch_models_is_best_effort_on_failure() -> None:
    client = _FakeClient(fail=True)

    assert await vllm.fetch_models(client, "http://127.0.0.1:8000/v1") is None


@pytest.mark.asyncio
async def test_fetch_version_reads_the_native_root_not_v1() -> None:
    client = _FakeClient(routes={"http://127.0.0.1:8000/version": {"version": "0.11.0"}})

    version = await vllm.fetch_version(client, "http://127.0.0.1:8000/v1")

    assert version == "0.11.0"
    assert client.requested == ["http://127.0.0.1:8000/version"]


@pytest.mark.asyncio
async def test_fetch_version_is_best_effort_on_failure() -> None:
    client = _FakeClient(fail=True)

    assert await vllm.fetch_version(client, "http://127.0.0.1:8000/v1") is None
