"""Contract tests for the llama.cpp dialect adapter, against RECORDED responses.

Fixtures live under ``tests/fixtures/capabilities/llama_cpp/`` (model-capabilities
brief Part 6 / 9.2: llama.cpp single and router mode). Covers ``/props``
(``default_generation_settings.n_ctx``, ``total_slots``, ``modalities.vision``,
``chat_template_caps`` verbatim + derived ``tools_enabled``, ``build_info``,
``model_path``), ``/v1/models`` (``meta.n_ctx_train``), and router-mode
``/models`` (``architecture.input_modalities`` + the ``status.args`` parser:
served context = ``--ctx-size / --parallel`` unless ``--kv-unified``, ``--jinja``
-> tools, ``--mmproj`` -> vision, ``--reasoning``/``--reasoning-budget`` ->
reasoning, ``--chat-template-kwargs`` -> default template kwargs).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from clio_agent.providers.capabilities.dialects import llama_cpp

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "capabilities" / "llama_cpp"


def _load(name: str) -> Any:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- /props (single mode)


def test_parse_props_reads_context_slots_and_template_caps() -> None:
    payload = _load("props_single.json")

    deployment = llama_cpp.parse_props(
        payload,
        provider_id="llama_cpp",
        api_base="http://127.0.0.1:9088/v1",
        model_id="qwen3-8b-q4_k_m.gguf",
    )

    assert deployment.context_served.value == 8192
    assert deployment.context_served.source == "server_report"
    assert deployment.slots.value == 4
    assert deployment.modalities_enabled.value == frozenset({"text"})
    assert deployment.tools_enabled.value is True
    assert deployment.template_caps.value == {
        "supports_tools": True,
        "supports_tool_calls": True,
        "supports_parallel_tool_calls": False,
        "supports_system_role": True,
        "supports_reasoning_effort": True,
        "supports_preserve_reasoning": True,
    }


def test_parse_props_derives_deployment_fingerprint_from_model_path() -> None:
    payload = _load("props_single.json")

    deployment = llama_cpp.parse_props(
        payload, provider_id="llama_cpp", api_base="http://127.0.0.1:9088/v1", model_id="m"
    )

    assert deployment.fingerprint == "llama_cpp:model_path=/models/qwen3-8b-q4_k_m.gguf"


def test_parse_props_links_model_key_via_gguf_filename() -> None:
    payload = _load("props_single.json")

    deployment = llama_cpp.parse_props(
        payload, provider_id="llama_cpp", api_base="http://127.0.0.1:9088/v1", model_id="local-model"
    )

    # the wire id ("local-model") matches no link rule on its own, but the
    # basename of /props' model_path IS a router-GGUF-shaped id once treated
    # as a candidate -- here it is not GGUF-router-shaped, so this falls back
    # to the wire id itself (documented "no link" outcome), never a guess.
    assert deployment.model_key.value == "local-model"
    assert "no link rule matched" in deployment.model_key.detail


def test_parse_props_missing_fields_are_unknown_not_false() -> None:
    deployment = llama_cpp.parse_props(
        {}, provider_id="llama_cpp", api_base="http://x/v1", model_id="m"
    )

    assert deployment.context_served.value is None
    assert deployment.slots.value is None
    assert deployment.modalities_enabled.value is None
    assert deployment.tools_enabled.value is None
    assert deployment.template_caps.value is None
    assert deployment.fingerprint == ""


def test_fingerprint_from_build_info_changes_when_build_changes() -> None:
    a = llama_cpp.fingerprint_from_build_info("5678 (b1234-abcdef0)")
    b = llama_cpp.fingerprint_from_build_info("5679 (b1235-fedcba0)")

    assert a != b
    assert a == llama_cpp.fingerprint_from_build_info("5678 (b1234-abcdef0)")


# --------------------------------------------------------------------------- /v1/models


def test_parse_v1_models_context_max_reads_n_ctx_train() -> None:
    payload = _load("v1_models.json")

    fact = llama_cpp.parse_v1_models_context_max(payload, "qwen3-8b-q4_k_m.gguf")

    assert fact.value == 40960
    assert fact.source == "server_report"


def test_parse_v1_models_context_max_unknown_for_unmatched_model() -> None:
    payload = _load("v1_models.json")

    fact = llama_cpp.parse_v1_models_context_max(payload, "some-other-model")

    assert fact.value is None


def test_build_model_capabilities_wires_context_max() -> None:
    payload = _load("v1_models.json")

    model = llama_cpp.build_model_capabilities("qwen3-8b", payload, "qwen3-8b-q4_k_m.gguf")

    assert model.model_key == "qwen3-8b"
    assert model.context_max.value == 40960


# --------------------------------------------------------------------------- router mode (/models)


def test_parse_status_args_extracts_llama_cpp_flags() -> None:
    args = [
        "--ctx-size", "16384",
        "--parallel", "2",
        "--jinja",
        "--reasoning", "on",
        "--chat-template-kwargs", '{"enable_thinking": true}',
    ]

    flags = llama_cpp.parse_status_args(args)

    assert flags.ctx_size == 16384
    assert flags.parallel == 2
    assert flags.jinja is True
    assert flags.kv_unified is False
    assert flags.reasoning == "on"
    assert flags.chat_template_kwargs == {"enable_thinking": True}


def test_parse_status_args_accepts_a_joined_string() -> None:
    flags = llama_cpp.parse_status_args("--ctx-size 8192 --mmproj /m/proj.gguf")

    assert flags.ctx_size == 8192
    assert flags.mmproj == "/m/proj.gguf"


def test_served_context_divides_by_parallel_by_default() -> None:
    flags = llama_cpp.RouterServerFlags(ctx_size=16384, parallel=2)

    assert llama_cpp.served_context_from_flags(flags) == 8192


def test_served_context_is_not_divided_when_kv_unified() -> None:
    flags = llama_cpp.RouterServerFlags(ctx_size=32768, parallel=4, kv_unified=True)

    assert llama_cpp.served_context_from_flags(flags) == 32768


def test_served_context_defaults_parallel_to_one() -> None:
    flags = llama_cpp.RouterServerFlags(ctx_size=8192)

    assert llama_cpp.served_context_from_flags(flags) == 8192


def test_parse_router_models_reads_both_rows() -> None:
    payload = _load("router_models.json")

    deployments = llama_cpp.parse_router_models(
        payload, provider_id="llama_cpp_router", api_base="http://127.0.0.1:9090"
    )

    assert set(deployments) == {"org/qwen3-8b-GGUF:Q4_K_M", "org/qwen3-vl-8b-GGUF:Q4_K_M"}

    text_only = deployments["org/qwen3-8b-GGUF:Q4_K_M"]
    assert text_only.context_served.value == 16384 // 2
    assert text_only.tools_enabled.value is True
    assert text_only.reasoning_enabled.value is True
    assert text_only.default_template_kwargs.value == {"enable_thinking": True}
    assert text_only.modalities_enabled.value == frozenset({"text"})

    vision = deployments["org/qwen3-vl-8b-GGUF:Q4_K_M"]
    assert vision.context_served.value == 32768  # kv-unified: not divided
    assert vision.modalities_enabled.value == frozenset({"text", "image"})
    assert vision.tools_enabled.value is False
    # the router-GGUF-shaped wire id links even with no overlay/HF layer wired
    assert vision.model_key.value == "org/qwen3-vl-8b"


def test_parse_router_model_row_links_gguf_router_id() -> None:
    payload = _load("router_models.json")
    row = payload["models"][0]

    deployment = llama_cpp.parse_router_model_row(
        row, provider_id="llama_cpp_router", api_base="http://127.0.0.1:9090"
    )

    assert deployment.model_key.value == "org/qwen3-8b"
    assert deployment.model_key.source == "server_report"


def test_deployment_fingerprint_from_args_is_stable_and_sensitive() -> None:
    a = llama_cpp.deployment_fingerprint_from_args(["--ctx-size", "8192"])
    b = llama_cpp.deployment_fingerprint_from_args(["--ctx-size", "16384"])

    assert a != b
    assert a == llama_cpp.deployment_fingerprint_from_args(["--ctx-size", "8192"])
    assert llama_cpp.deployment_fingerprint_from_args(None) == ""


# --------------------------------------------------------------------------- endpoint


def test_build_endpoint_capabilities_reports_fingerprint_and_version() -> None:
    endpoint = llama_cpp.build_endpoint_capabilities(
        "llama_cpp", "http://127.0.0.1:9088/v1", "qwen3-8b", build_info="5678 (b1234-abcdef0)"
    )

    assert endpoint.dialect == "llama_cpp"
    assert endpoint.fingerprint == "llama_cpp:build_info=5678 (b1234-abcdef0)"
    assert endpoint.server_version.value == "5678 (b1234-abcdef0)"
    # the shared supplement table (brief 5.2 step 3) still fills accepted_params
    assert endpoint.accepted_params.known
    assert "reasoning_effort" in (endpoint.accepted_params.value or set())


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
async def test_fetch_props_single_mode_is_unqualified() -> None:
    payload = _load("props_single.json")
    client = _FakeClient(routes={"http://127.0.0.1:9088/props": payload})

    data = await llama_cpp.fetch_props(client, "http://127.0.0.1:9088")

    assert data == payload
    assert client.requested == ["http://127.0.0.1:9088/props"]


@pytest.mark.asyncio
async def test_fetch_props_router_mode_queries_only_when_loaded() -> None:
    client = _FakeClient(routes={"http://127.0.0.1:9090/props?model=m": {"ok": True}})

    data = await llama_cpp.fetch_props(client, "http://127.0.0.1:9090", model_id="m", loaded=True)

    assert data == {"ok": True}

    # NOT loaded -> must stay on the safe, unqualified form (never query an
    # unloaded router model, which would load it as a side effect).
    client2 = _FakeClient(routes={"http://127.0.0.1:9090/props": {"safe": True}})
    data2 = await llama_cpp.fetch_props(client2, "http://127.0.0.1:9090", model_id="m", loaded=False)
    assert data2 == {"safe": True}
    assert client2.requested == ["http://127.0.0.1:9090/props"]


@pytest.mark.asyncio
async def test_fetch_props_is_best_effort_on_failure() -> None:
    client = _FakeClient(fail=True)

    assert await llama_cpp.fetch_props(client, "http://127.0.0.1:9088") is None


@pytest.mark.asyncio
async def test_fetch_v1_models_reads_the_endpoint() -> None:
    payload = _load("v1_models.json")
    client = _FakeClient(routes={"http://127.0.0.1:9088/v1/models": payload})

    data = await llama_cpp.fetch_v1_models(client, "http://127.0.0.1:9088")

    assert data == payload


@pytest.mark.asyncio
async def test_fetch_router_models_reads_the_endpoint() -> None:
    payload = _load("router_models.json")
    client = _FakeClient(routes={"http://127.0.0.1:9090/models": payload})

    data = await llama_cpp.fetch_router_models(client, "http://127.0.0.1:9090")

    assert data == payload
