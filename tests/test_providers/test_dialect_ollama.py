"""Contract tests for the Ollama dialect adapter, against RECORDED responses.

Fixtures: the existing ``tests/test_providers/fixtures/handshake/ollama_api_show_qwen3.json``
(P4a's) plus ``tests/fixtures/capabilities/ollama/`` (``/api/ps``,
``/api/version``). This module is the SINGLE reader of Ollama's native API
(consolidation review of #1447: ``handshake/ollama.py`` used to read/parse
``/api/show`` itself); it reads via plain ``client.get``/``client.post`` --
the SAME ``httpx.AsyncClient``-shaped interface every dialect adapter and
:class:`~clio_agent.providers.handshake.base.ProviderHandshake` phase already
shares, not the official ``ollama`` Python package's client (which has no
generic HTTP verb methods and so cannot be threaded through the one shared
client every handshake phase uses without a second, incompatible client type).

Covers the field mapping the brief calls out for Ollama (Part 6):
``model_info.<arch>.context_length`` -> the model's own ceiling,
``capabilities`` -> tools/thinking/input-modalities, the Modelfile
``parameters`` blob, the loaded context from ``/api/ps``
(``ollama-probe-context``), and the endpoint fingerprint from ``/api/version``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from clio_agent.providers.capabilities.dialects import ollama as ollama_dialect

HANDSHAKE_FIXTURES = Path(__file__).parent / "fixtures" / "handshake"
CAPABILITY_FIXTURES = Path(__file__).parent.parent / "fixtures" / "capabilities" / "ollama"
ROOT = "http://127.0.0.1:11434"


def _load(directory: Path, name: str) -> Any:
    return json.loads((directory / name).read_text(encoding="utf-8"))


@dataclass
class _FakeResponse:
    status_code: int
    _payload: Any = None

    def json(self) -> Any:
        return self._payload


class _FakeAsyncClient:
    """In-memory fake serving the recorded Ollama fixtures by URL/method."""

    def __init__(self, *, show: Any = None, ps: Any = None, version: Any = None, fail: bool = False) -> None:
        self._show = show
        self._ps = ps
        self._version = version
        self._fail = fail
        self.requested: list[str] = []

    async def get(self, url: str, **_: object) -> _FakeResponse:
        self.requested.append(url)
        if self._fail:
            raise ConnectionError("unreachable")
        if url.endswith("/api/ps"):
            return _FakeResponse(200, self._ps) if self._ps is not None else _FakeResponse(404)
        if url.endswith("/api/version"):
            return _FakeResponse(200, self._version) if self._version is not None else _FakeResponse(404)
        if url.endswith("/api/tags"):
            return _FakeResponse(200, self._tags) if hasattr(self, "_tags") else _FakeResponse(404)
        return _FakeResponse(404)

    async def post(self, url: str, json: dict[str, Any], **_: object) -> _FakeResponse:
        self.requested.append(url)
        if self._fail:
            raise ConnectionError("unreachable")
        if url.endswith("/api/show"):
            return _FakeResponse(200, self._show) if self._show is not None else _FakeResponse(404)
        return _FakeResponse(404)


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


def test_show_identity_reads_arch_and_capabilities() -> None:
    show = _load(HANDSHAKE_FIXTURES, "ollama_api_show_qwen3.json")

    arch, caps = ollama_dialect.show_identity(show)

    assert arch == "qwen3"
    assert caps == ("completion", "tools", "thinking")


def test_show_identity_defensive_on_non_dict() -> None:
    assert ollama_dialect.show_identity(None) == (None, ())
    assert ollama_dialect.show_identity("not a dict") == (None, ())


def test_parse_show_maps_context_tools_and_thinking() -> None:
    show = _load(HANDSHAKE_FIXTURES, "ollama_api_show_qwen3.json")

    model = ollama_dialect.parse_show(show, model_key="qwen3:8b")

    assert model.context_max.value == 40960
    assert model.context_max.source == "server_report"
    assert "model_info" in model.context_max.detail
    assert model.tools.value is True
    assert model.thinking.known
    assert model.thinking.value.mechanism == "on_off"
    assert model.input_modalities.value == frozenset({"text"})


def test_parse_show_missing_data_is_unknown_not_false() -> None:
    model = ollama_dialect.parse_show(None, model_key="qwen3:8b")

    assert not model.context_max.known
    assert not model.tools.known
    assert not model.input_modalities.known
    assert not model.thinking.known


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


# --------------------------------------------------------------------------- fetch (fake HTTP client)


@pytest.mark.asyncio
async def test_fetch_show_reads_the_native_endpoint() -> None:
    show_payload = _load(HANDSHAKE_FIXTURES, "ollama_api_show_qwen3.json")
    client = _FakeAsyncClient(show=show_payload)

    data = await ollama_dialect.fetch_show(client, ROOT, "qwen3:8b")

    assert data == show_payload
    assert client.requested == [f"{ROOT}/api/show"]


@pytest.mark.asyncio
async def test_fetch_show_is_best_effort_on_failure() -> None:
    client = _FakeAsyncClient(fail=True)

    assert await ollama_dialect.fetch_show(client, ROOT, "qwen3:8b") is None


@pytest.mark.asyncio
async def test_fetch_show_none_on_404() -> None:
    client = _FakeAsyncClient(show=None)

    assert await ollama_dialect.fetch_show(client, ROOT, "unknown:model") is None


@pytest.mark.asyncio
async def test_fetch_ps_reads_the_native_endpoint() -> None:
    ps_payload = _load(CAPABILITY_FIXTURES, "api_ps.json")
    client = _FakeAsyncClient(ps=ps_payload)

    data = await ollama_dialect.fetch_ps(client, ROOT)

    assert data == ps_payload


@pytest.mark.asyncio
async def test_fetch_ps_is_best_effort_on_failure() -> None:
    client = _FakeAsyncClient(fail=True)

    assert await ollama_dialect.fetch_ps(client, ROOT) is None


@pytest.mark.asyncio
async def test_fetch_version_reads_the_native_endpoint() -> None:
    client = _FakeAsyncClient(version={"version": "0.5.4"})

    version = await ollama_dialect.fetch_version(client, ROOT)

    assert version == "0.5.4"


@pytest.mark.asyncio
async def test_fetch_version_is_best_effort_on_failure() -> None:
    client = _FakeAsyncClient(fail=True)

    assert await ollama_dialect.fetch_version(client, ROOT) is None


@pytest.mark.asyncio
async def test_fetch_tags_normalizes_rows_and_filters_non_dicts() -> None:
    client = _FakeAsyncClient()
    client._tags = {"models": [{"model": "qwen3:8b"}, {"name": "llama3.2"}]}  # type: ignore[attr-defined]

    rows = await ollama_dialect.fetch_tags(client, ROOT)

    assert [r["id"] for r in rows] == ["qwen3:8b", "llama3.2"]


@pytest.mark.asyncio
async def test_fetch_tags_empty_on_failure() -> None:
    client = _FakeAsyncClient(fail=True)

    assert await ollama_dialect.fetch_tags(client, ROOT) == []


def test_parse_show_capabilities_name_the_task() -> None:
    """Ollama's exhaustive capabilities list names the task: an embedding model
    is a surrogate (feature-extraction), a completion model generates text."""
    embed = ollama_dialect.parse_show({"capabilities": ["embedding"]}, model_key="nomic-embed-text")
    assert embed.task.value == "feature-extraction"
    chat = ollama_dialect.parse_show({"capabilities": ["completion", "tools"]}, model_key="qwen3:8b")
    assert chat.task.value == "text-generation"
    assert not ollama_dialect.parse_show({}, model_key="x").task.known
