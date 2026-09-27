"""What a local server actually serves vs. what the model is: context basis and parameter scope.

Each case reproduces a finding from the managed-server run on ares
(2026-09-27) with the servers' real payloads.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from clio_agent.gact.catalog_context import context_basis, context_wire
from clio_agent.providers.capabilities import invalidation, server_defaults
from clio_agent.providers.capabilities.combine import combine_capabilities
from clio_agent.providers.capabilities.dialects import llama_cpp
from clio_agent.providers.capabilities.model_facts import ParameterCount
from clio_agent.providers.capabilities.model_sources import model_parameter_count
from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    Fact,
    ModelCapabilities,
    unknown,
)
from clio_agent.providers.handshake import vllm_tools

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "managed_servers"
_NOW = "2026-09-27T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _reset() -> None:
    invalidation.clear_all()
    vllm_tools._verdicts.clear()  # noqa: SLF001
    yield
    invalidation.clear_all()
    vllm_tools._verdicts.clear()  # noqa: SLF001


# ------------------------------------------------------------------ parameter scope


def _hub(total: int) -> ModelCapabilities:
    return ModelCapabilities(
        model_key="Qwen/Qwen2.5-0.5B-Instruct",
        parameters=Fact(ParameterCount(total=total), "hf_repo", _NOW, f"safetensors total={total}"),
    )


def test_a_gguf_storing_the_tied_embedding_twice_yields_to_the_models_own_count() -> None:
    payload = json.loads((FIXTURES / "llama_cpp_b11206_models.json").read_text())
    model_id = payload["data"][0]["id"]
    served = llama_cpp.build_model_capabilities("Qwen/Qwen2.5-0.5B-Instruct", payload, model_id)
    assert served.parameters.value.total == 630_167_424
    assert served.parameters.value.scope == "file"

    chosen = model_parameter_count(served.parameters, _hub(494_032_768))

    assert chosen.value.total == 494_032_768
    assert chosen.source == "hf_repo"
    # llama.cpp's own number is kept, labelled, with the exact reason.
    assert "the served file stores 630167424 weights" in chosen.detail
    assert "tied embedding matrix a second time" in chosen.detail


def test_a_file_count_stands_when_no_model_count_is_known_and_a_model_count_is_untouched() -> None:
    payload = json.loads((FIXTURES / "llama_cpp_b11206_models.json").read_text())
    served = llama_cpp.build_model_capabilities("m", payload, payload["data"][0]["id"])

    assert model_parameter_count(served.parameters, None) is served.parameters
    model_scope = Fact(ParameterCount(total=5), "overlay", _NOW, "curated")
    assert model_parameter_count(model_scope, _hub(7)) is model_scope


def test_an_unexplained_difference_is_stated_as_a_count_not_a_cause() -> None:
    file_count = Fact(
        ParameterCount(total=1100, scope="file", embedding_elements=50), "server_report", _NOW, "n"
    )

    chosen = model_parameter_count(file_count, _hub(1000))

    assert "+100 weights" in chosen.detail
    assert "tied embedding" not in chosen.detail


# ------------------------------------------------------------------ context basis


def _deployment(**facts: Fact) -> DeploymentCapabilities:
    return DeploymentCapabilities(
        provider_id="ollama",
        api_base="http://127.0.0.1:11434",
        model_id="qwen2.5:0.5b",
        model_key=Fact("qwen2.5:0.5b", "server_report", _NOW),
        **facts,
    )


_MODEL = ModelCapabilities(model_key="qwen2.5:0.5b", context_max=Fact(32768, "hf_repo", _NOW))


def test_before_load_the_configured_context_is_in_force_and_the_native_one_is_kept() -> None:
    deployment = _deployment(
        context_configured=Fact(4096, "server_report", _NOW, "OLLAMA_CONTEXT_LENGTH")
    )
    invalidation.record_model_capabilities(_MODEL)
    effective = combine_capabilities(_MODEL, None, deployment)

    wire = context_wire(effective, deployment)

    assert wire == {
        "context_window": 4096,
        "loaded_context_window": None,
        "native_context_window": 32768,
        "context_basis": "configured",
    }


def test_once_loaded_the_served_context_wins_over_anything_configured() -> None:
    deployment = _deployment(
        context_served=Fact(8192, "server_report", _NOW, "/api/ps"),
        context_configured=Fact(4096, "server_report", _NOW, "default"),
    )
    effective = combine_capabilities(_MODEL, None, deployment)

    assert effective.context.value == 8192
    assert context_basis(effective.context.decided_by) == "served"


def test_with_nothing_served_or_configured_the_number_is_labelled_native() -> None:
    effective = combine_capabilities(_MODEL, None, _deployment())

    assert effective.context.value == 32768
    assert context_basis(effective.context.decided_by) == "native"
    assert context_basis(combine_capabilities(None, None, None).context.decided_by) is None


def test_the_server_default_lookup_matches_by_native_root_and_owner_replaces() -> None:
    fact = Fact(4096, "server_report", _NOW, "OLLAMA_CONTEXT_LENGTH=4096")
    server_defaults.register_context_default_lookup(
        "test", lambda root: fact if root == "http://127.0.0.1:52011" else None
    )
    try:
        assert server_defaults.context_default("http://127.0.0.1:52011/v1") == fact
        assert server_defaults.context_default("http://127.0.0.1:1") is None
        server_defaults.register_context_default_lookup("test", lambda root: None)
        assert server_defaults.context_default("http://127.0.0.1:52011") is None
    finally:
        server_defaults.unregister_context_default_lookup("test")


# ------------------------------------------------------------------ vLLM tools, verified


def _vllm(*, refuse: bool, started: str = "1759000000.5") -> tuple[httpx.AsyncClient, list[str]]:
    calls: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/metrics":
            return httpx.Response(200, text=f"process_start_time_seconds {started}\n")
        if refuse:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": '"auto" tool choice requires --enable-auto-tool-choice '
                        "and --tool-call-parser to be set"
                    }
                },
            )
        call = {"function": {"name": "record_sum", "arguments": '{"a": 21, "b": 21}'}}
        return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [call]}}]})

    return httpx.AsyncClient(transport=httpx.MockTransport(answer)), calls


@pytest.mark.asyncio
async def test_a_vllm_started_without_a_tool_parser_is_recorded_as_not_serving_tools() -> None:
    client, _ = _vllm(refuse=True)
    async with client:
        fact = await vllm_tools.vllm_tools_fact(client, "http://127.0.0.1:8000/v1", "q")

    assert fact.value is False
    model = ModelCapabilities(model_key="q", tools=Fact(True, "hf_repo", _NOW, "template"))
    deployment = DeploymentCapabilities(
        provider_id="vllm", api_base="http://127.0.0.1:8000/v1", model_id="q", tools_enabled=fact
    )
    assert combine_capabilities(model, None, deployment).tools.value is False


@pytest.mark.asyncio
async def test_the_probe_runs_once_per_server_process() -> None:
    client, calls = _vllm(refuse=False)
    async with client:
        first = await vllm_tools.vllm_tools_fact(client, "http://127.0.0.1:8000/v1", "q")
        second = await vllm_tools.vllm_tools_fact(client, "http://127.0.0.1:8000/v1", "q")

    assert first.value is True and second is first
    assert calls.count("/v1/chat/completions") == 1

    restarted, calls = _vllm(refuse=True, started="1759999999.0")
    async with restarted:
        again = await vllm_tools.vllm_tools_fact(restarted, "http://127.0.0.1:8000/v1", "q")
    assert again.value is False
    assert calls.count("/v1/chat/completions") == 1


@pytest.mark.asyncio
async def test_an_undecided_probe_stays_unknown_and_is_not_cached() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/metrics":
            return httpx.Response(200, text="process_start_time_seconds 1.0\n")
        raise httpx.ReadTimeout("slow CPU generation")

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        fact = await vllm_tools.vllm_tools_fact(client, "http://127.0.0.1:8000/v1", "q")

    assert fact == unknown(fact.detail) and not fact.known
    assert vllm_tools._verdicts == {}  # noqa: SLF001
