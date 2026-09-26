"""Contract tests for the LM Studio dialect adapter, against RECORDED responses.

``tests/fixtures/capabilities/lm_studio/api_v1_models.json`` is brief-shaped
(model-capabilities brief Part 6: ``max_context_length``, ``loaded_context_length``,
``capabilities.vision``, ``trained_for_tool_use``, ``reasoning.allowed_options``)
rather than captured from a live server -- no real network probe against a user
service is available in this environment, and the brief itself is the primary
source for this exact field shape. The v0 fallback fixture IS a previously
recorded real payload
(``tests/test_providers/fixtures/handshake/lmstudio_v0_models.json``, P4a's).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from clio_agent.providers.capabilities.dialects import lm_studio

V1_FIXTURES = Path(__file__).parent.parent / "fixtures" / "capabilities" / "lm_studio"
V0_FIXTURES = Path(__file__).parent / "fixtures" / "handshake"


def _load(directory: Path, name: str) -> Any:
    return json.loads((directory / name).read_text(encoding="utf-8"))


def test_parse_v1_row_reads_context_tools_vision_and_reasoning_options() -> None:
    row = _load(V1_FIXTURES, "api_v1_models.json")["data"][0]

    model, deployment = lm_studio.parse_v1_row(row, provider_id="lm_studio", api_base="http://127.0.0.1:1234/v1")

    assert model.context_max.value == 40960
    assert model.tools.value is True
    assert model.input_modalities.value == frozenset({"text"})
    assert deployment.context_served.value == 8192
    assert deployment.template_caps.value == {"reasoning_allowed_options": ["low", "medium", "high"]}
    assert deployment.fingerprint == "lm_studio:model=qwen/qwen3-8b:loaded_context=8192"


def test_parse_v1_row_vision_true() -> None:
    row = {
        "id": "vlm/model",
        "max_context_length": 8192,
        "loaded_context_length": 8192,
        "capabilities": {"vision": True},
        "trained_for_tool_use": False,
    }

    model, _deployment = lm_studio.parse_v1_row(row, provider_id="lm_studio", api_base="http://x/v1")

    assert model.input_modalities.value == frozenset({"text", "image"})
    assert model.tools.value is False


def test_parse_v1_row_missing_fields_are_unknown() -> None:
    model, deployment = lm_studio.parse_v1_row({"id": "bare"}, provider_id="lm_studio", api_base="http://x/v1")

    assert not model.context_max.known
    assert not model.tools.known
    assert not model.input_modalities.known
    assert not deployment.context_served.known
    assert not deployment.template_caps.known


def test_build_endpoint_capabilities_never_sends_chat_template_kwargs() -> None:
    endpoint = lm_studio.build_endpoint_capabilities("lm_studio", "http://127.0.0.1:1234/v1", "m")

    assert endpoint.dialect == "lm_studio"
    assert "chat_template_kwargs" not in (endpoint.accepted_params.value or set())


# --------------------------------------------------------------------------- fetch_rows: v1-then-v0 fallback


@dataclass
class _FakeResponse:
    status_code: int
    _payload: Any = None

    def json(self) -> Any:
        return self._payload


class _FakeClient:
    def __init__(self, *, v1: Any = None, v0: Any = None) -> None:
        self._v1 = v1
        self._v0 = v0
        self.requested: list[str] = []

    async def get(self, url: str, **_: object) -> _FakeResponse:
        self.requested.append(url)
        if url.endswith("/api/v1/models"):
            if self._v1 is None:
                return _FakeResponse(404)
            return _FakeResponse(200, self._v1)
        if url.endswith("/api/v0/models"):
            if self._v0 is None:
                return _FakeResponse(404)
            return _FakeResponse(200, self._v0)
        return _FakeResponse(404)


@pytest.mark.asyncio
async def test_fetch_rows_prefers_v1_when_available() -> None:
    v1_payload = _load(V1_FIXTURES, "api_v1_models.json")
    client = _FakeClient(v1=v1_payload, v0={"data": []})

    rows, schema = await lm_studio.fetch_rows(client, "http://127.0.0.1:1234/v1")

    assert schema == "v1"
    assert rows[0]["id"] == "qwen/qwen3-8b"
    assert client.requested == ["http://127.0.0.1:1234/api/v1/models"]


@pytest.mark.asyncio
async def test_fetch_rows_falls_back_to_v0_when_v1_is_absent() -> None:
    v0_payload = _load(V0_FIXTURES, "lmstudio_v0_models.json")
    client = _FakeClient(v1=None, v0=v0_payload)

    rows, schema = await lm_studio.fetch_rows(client, "http://127.0.0.1:1234/v1")

    assert schema == "v0"
    assert {r["id"] for r in rows} == {
        "qwopus3.5-9b-v3",
        "text-embedding-nomic-embed-text-v1.5",
        "ibm/granite-4-h-tiny",
    }


@pytest.mark.asyncio
async def test_fetch_rows_empty_when_neither_endpoint_answers() -> None:
    client = _FakeClient(v1=None, v0=None)

    rows, schema = await lm_studio.fetch_rows(client, "http://127.0.0.1:1234/v1")

    assert rows == []
    assert schema == ""
