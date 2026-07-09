"""Offline tests for :class:`ArgonneHandshake`.

Every test loads captured ALCF fixtures from disk and injects them through a
fake ``httpx.AsyncClient`` (or by monkeypatching ``discover_models``); nothing
ever touches the network. ``argonne_auth`` is monkeypatched so token resolution
is deterministic and never spawns a Globus flow.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from clio_agent.providers import argonne_auth
from clio_agent.providers.handshake.argonne import ArgonneHandshake
from clio_agent.providers.handshake.base import HandshakeContext
from clio_agent.providers.handshake.model import (
    AuthState,
    ConnectivityState,
)

FIXTURES = Path(__file__).parent / "fixtures" / "handshake"
SOPHIA_API_BASE = "https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1"


def _load(name: str) -> Any:
    """Load a captured JSON fixture from disk."""
    return json.loads((FIXTURES / name).read_text())


class _FakeResponse:
    """Minimal stand-in for an ``httpx.Response`` over captured JSON."""

    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    """Fake ``httpx.AsyncClient`` that serves fixtures by URL suffix.

    Records every requested URL so tests can assert that *no* request was made
    in passive/no-token mode.
    """

    def __init__(self, routes: dict[str, Any]) -> None:
        self._routes = routes
        self.calls: list[str] = []

    async def get(self, url: str, headers: dict[str, str] | None = None) -> _FakeResponse:
        self.calls.append(url)
        for suffix, payload in self._routes.items():
            if url.endswith(suffix):
                return _FakeResponse(payload)
        raise AssertionError(f"unexpected URL requested: {url}")


def _sophia_context(auth_mode: str = "active") -> HandshakeContext:
    return HandshakeContext(
        provider_id="argonne",
        provider_kind="argonne",
        api_base=SOPHIA_API_BASE,
        auth_mode=auth_mode,
        extra={"auth_header": {"Authorization": "Bearer fixture-token"}},
    )


@pytest.fixture(autouse=True)
def _no_globus(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: no env token, no stored Globus token, no token minting.

    Individual tests override these as needed. This guarantees that any
    accidental real call into ``argonne_auth`` is inert and offline.
    """
    for var in ("CLIO_ARGONNE_TOKEN", "ALCF_INFERENCE_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(argonne_auth, "tokens_exist", lambda: False)

    def _boom(*_a: Any, **_k: Any) -> str:
        raise AssertionError("get_access_token must not be called in this test")

    monkeypatch.setattr(argonne_auth, "get_access_token", _boom)


# --------------------------------------------------------------------------- #
# api_base splitting
# --------------------------------------------------------------------------- #


def test_split_api_base_sophia() -> None:
    root, cluster = ArgonneHandshake._split_api_base(SOPHIA_API_BASE)
    assert root == "https://inference-api.alcf.anl.gov/resource_server"
    assert cluster == "sophia"


def test_split_api_base_metis() -> None:
    base = "https://inference-api.alcf.anl.gov/resource_server/metis/api/v1"
    _root, cluster = ArgonneHandshake._split_api_base(base)
    assert cluster == "metis"


def test_split_api_base_rejects_bad_base() -> None:
    with pytest.raises(ValueError):
        ArgonneHandshake._split_api_base("https://example.com/v1")


# --------------------------------------------------------------------------- #
# discover_model_config — the core mapping contract
# --------------------------------------------------------------------------- #


async def _profiles_for(rows: list[dict[str, Any]]) -> dict[str, Any]:
    hs = ArgonneHandshake(provider=None)
    ctx = _sophia_context()
    out = {}
    for row in rows:
        profile = await hs.discover_model_config(client=None, ctx=ctx, raw=row)
        out[profile.id] = profile
    return out


@pytest.mark.asyncio
async def test_model_config_mapping_from_fixture() -> None:
    rows = _load("alcf_sophia_models.json")
    profiles = await _profiles_for(rows)

    nemotron = profiles["nvidia/nemotron-3-super-120b"]
    assert nemotron.context_window == 262144
    assert nemotron.is_reasoning is True
    assert nemotron.reasoning_param == "super_v3"
    # enable_auto_tool_choice is true even though no tool_call_parser is set.
    assert nemotron.native_tool_calling is True
    assert nemotron.context_source == "live"

    gpt_oss = profiles["openai/gpt-oss-120b"]
    assert gpt_oss.context_window == 65536
    assert gpt_oss.native_tool_calling is True
    assert gpt_oss.tool_call_parser == "openai"
    assert gpt_oss.is_reasoning is False

    # A bare row with no config: everything falls back to None/False.
    bare = profiles["argonne/AuroraGPT-IT-v4-0125"]
    assert bare.context_window is None
    assert bare.is_reasoning is False
    assert bare.native_tool_calling is False


# --------------------------------------------------------------------------- #
# discover_models — list + hot-model annotation from /jobs
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_discover_models_marks_hot_from_jobs() -> None:
    client = _FakeClient(
        {
            "/sophia/models": _load("alcf_sophia_models.json"),
            "/sophia/jobs": _load("alcf_sophia_jobs.json"),
        }
    )
    hs = ArgonneHandshake(provider=None)
    rows = await hs.discover_models(client, _sophia_context())

    by_id = {r["id"]: r for r in rows}
    # nemotron is in the running jobs fixture -> marked loaded.
    assert by_id["nvidia/nemotron-3-super-120b"].get("is_loaded") is True
    # gpt-oss-120b is comma-joined with gpt-oss-20b in a running job.
    assert by_id["openai/gpt-oss-120b"].get("is_loaded") is True
    assert by_id["openai/gpt-oss-20b"].get("is_loaded") is True
    # A model with no running job is not marked loaded.
    assert "is_loaded" not in by_id["mistralai/Mixtral-8x22B-Instruct-v0.1"]


@pytest.mark.asyncio
async def test_discover_models_survives_jobs_failure() -> None:
    """A missing /jobs endpoint loses hot annotation but not the model list."""

    class _Jobsless(_FakeClient):
        async def get(self, url: str, headers: dict[str, str] | None = None) -> _FakeResponse:
            self.calls.append(url)
            if url.endswith("/jobs"):
                return _FakeResponse(None, status_code=500)
            return await super().get(url, headers)

    client = _Jobsless({"/sophia/models": _load("alcf_sophia_models.json")})
    hs = ArgonneHandshake(provider=None)
    rows = await hs.discover_models(client, _sophia_context())
    assert any(r["id"] == "openai/gpt-oss-120b" for r in rows)
    assert all("is_loaded" not in r for r in rows)


# --------------------------------------------------------------------------- #
# check_connectivity — auth-mode aware, OAuth-safe
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_passive_no_token_skips_with_zero_network_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passive mode + no token -> SKIPPED/MISSING and no client.get calls."""
    hs = ArgonneHandshake(provider=None)
    ctx = HandshakeContext(
        provider_id="argonne",
        provider_kind="argonne",
        api_base=SOPHIA_API_BASE,
        auth_mode="passive",
    )
    client = _FakeClient({})
    result = await hs.check_connectivity(client, ctx)

    assert result.connectivity is ConnectivityState.SKIPPED
    assert result.auth is AuthState.MISSING
    assert result.auth_header == {}
    # The load-bearing assertion: zero network calls in passive/no-token mode.
    assert client.calls == []


@pytest.mark.asyncio
async def test_passive_with_stored_token_skips_deferred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stored Globus token that cannot validate offline -> SKIPPED/DEFERRED."""
    monkeypatch.setattr(argonne_auth, "tokens_exist", lambda: True)

    def _fail(_force: bool = False, *, allow_interactive: bool = True) -> str:
        assert allow_interactive is False
        raise RuntimeError("offline: cannot refresh")

    monkeypatch.setattr(argonne_auth, "get_access_token", _fail)

    hs = ArgonneHandshake(provider=None)
    ctx = HandshakeContext(
        provider_id="argonne",
        provider_kind="argonne",
        api_base=SOPHIA_API_BASE,
        auth_mode="passive",
    )
    client = _FakeClient({})
    result = await hs.check_connectivity(client, ctx)

    assert result.connectivity is ConnectivityState.SKIPPED
    assert result.auth is AuthState.DEFERRED
    assert client.calls == []


@pytest.mark.asyncio
async def test_passive_env_token_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """An env token makes passive mode OK with a Bearer header, no network."""
    monkeypatch.setenv("CLIO_ARGONNE_TOKEN", "env-token-123")
    hs = ArgonneHandshake(provider=None)
    ctx = HandshakeContext(
        provider_id="argonne",
        provider_kind="argonne",
        api_base=SOPHIA_API_BASE,
        auth_mode="passive",
    )
    client = _FakeClient({})
    result = await hs.check_connectivity(client, ctx)

    assert result.connectivity is ConnectivityState.OK
    assert result.auth is AuthState.OK
    assert result.auth_header == {"Authorization": "Bearer env-token-123"}
    assert client.calls == []


@pytest.mark.asyncio
async def test_active_mode_refreshes_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Active mode with no env/stored token may mint via a non-interactive refresh."""
    monkeypatch.setattr(argonne_auth, "tokens_exist", lambda: False)
    monkeypatch.setattr(
        argonne_auth,
        "get_access_token",
        lambda _force=False, *, allow_interactive=True: "active-token",
    )

    hs = ArgonneHandshake(provider=None)
    ctx = HandshakeContext(
        provider_id="argonne",
        provider_kind="argonne",
        api_base=SOPHIA_API_BASE,
        auth_mode="active",
    )
    client = _FakeClient({})
    result = await hs.check_connectivity(client, ctx)

    assert result.connectivity is ConnectivityState.OK
    assert result.auth is AuthState.OK
    assert result.auth_header == {"Authorization": "Bearer active-token"}
    assert client.calls == []


# --------------------------------------------------------------------------- #
# full handshake — monkeypatch discover_models to feed the fixture
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_full_handshake_monkeypatched_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end with an env token; discover_models fed from the fixture."""
    monkeypatch.setenv("CLIO_ARGONNE_TOKEN", "env-token")

    rows = _load("alcf_sophia_models.json")

    async def _fake_discover(
        self: ArgonneHandshake, client: Any, ctx: HandshakeContext
    ) -> list[dict[str, Any]]:
        return rows

    monkeypatch.setattr(ArgonneHandshake, "discover_models", _fake_discover)

    hs = ArgonneHandshake(provider=None)
    ctx = HandshakeContext(
        provider_id="argonne",
        provider_kind="argonne",
        api_base=SOPHIA_API_BASE,
        auth_mode="passive",
        allow_external_sources=False,  # keep it fully offline; no models.dev
    )
    report = await hs.handshake(ctx)

    assert report.connectivity is ConnectivityState.OK
    assert report.auth is AuthState.OK
    nemotron = report.model("nvidia/nemotron-3-super-120b")
    assert nemotron is not None
    assert nemotron.context_window == 262144
    assert nemotron.is_reasoning is True
    assert report.model("openai/gpt-oss-120b").context_window == 65536
