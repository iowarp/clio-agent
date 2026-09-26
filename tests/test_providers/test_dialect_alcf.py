"""Unit tests for the ALCF dialect wrapper's best-effort per-job vLLM probe.

No recorded ALCF fixture exists for a job's own endpoint shape (an
authenticated Globus gateway this environment cannot reach), so this exercises
the wrapper's CONTRACT -- it never raises, degrades to ``None`` whenever a job
names no endpoint or the probe fails, and otherwise defers entirely to
:mod:`clio_agent.providers.capabilities.dialects.vllm`'s own parsing -- with a
tiny fake HTTP client.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from clio_agent.providers.capabilities.dialects import alcf


@dataclass
class _FakeResponse:
    status_code: int
    _payload: Any = None

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    def __init__(self, *, models: Any = None, version: Any = None, fail: bool = False) -> None:
        self._models = models
        self._version = version
        self._fail = fail

    async def get(self, url: str, **_: object) -> _FakeResponse:
        if self._fail:
            raise ConnectionError("unreachable")
        if url.endswith("/v1/models"):
            return _FakeResponse(200, self._models)
        if url.endswith("/version"):
            return _FakeResponse(200, self._version)
        return _FakeResponse(404)


def test_job_endpoint_url_tries_known_field_names() -> None:
    assert alcf.job_endpoint_url({"endpoint": "http://10.0.0.1:8000"}) == "http://10.0.0.1:8000"
    assert alcf.job_endpoint_url({"url": "http://10.0.0.2:8000"}) == "http://10.0.0.2:8000"
    assert alcf.job_endpoint_url({}) is None
    assert alcf.job_endpoint_url({"endpoint": "  "}) is None


@pytest.mark.asyncio
async def test_probe_job_endpoint_returns_none_when_job_names_no_endpoint() -> None:
    result = await alcf.probe_job_endpoint(
        _FakeClient(), {}, provider_id="argonne_sophia", model_id="m"
    )

    assert result is None


@pytest.mark.asyncio
async def test_probe_job_endpoint_is_best_effort_on_failure() -> None:
    job = {"endpoint": "http://10.0.0.1:8000"}
    result = await alcf.probe_job_endpoint(
        _FakeClient(fail=True), job, provider_id="argonne_sophia", model_id="m"
    )

    assert result is None


@pytest.mark.asyncio
async def test_probe_job_endpoint_builds_deployment_and_endpoint_on_success() -> None:
    job = {"endpoint": "http://10.0.0.1:8000"}
    client = _FakeClient(
        models={"data": [{"id": "m", "root": "Qwen/Qwen3-8B", "max_model_len": 40960}]},
        version={"version": "0.11.0"},
    )

    result = await alcf.probe_job_endpoint(client, job, provider_id="argonne_sophia", model_id="m")

    assert result is not None
    endpoint, deployment = result
    assert endpoint.dialect == "vllm"
    assert endpoint.fingerprint == "vllm:version=0.11.0"
    assert deployment.context_served.value == 40960
    assert deployment.model_key.value == "Qwen/Qwen3-8B"


@pytest.mark.asyncio
async def test_probe_job_endpoint_returns_none_when_model_row_missing() -> None:
    job = {"endpoint": "http://10.0.0.1:8000"}
    client = _FakeClient(models={"data": []}, version={"version": "0.11.0"})

    result = await alcf.probe_job_endpoint(client, job, provider_id="argonne_sophia", model_id="m")

    assert result is None
