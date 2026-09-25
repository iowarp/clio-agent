"""Focused contract tests for the restored Codex SDK transport (S1b).

Every test here fakes the ``openai_codex`` SDK boundary (never a real
subprocess or network call) and points ``CODEX_HOME`` at a throwaway
directory as a defensive measure -- the whole point of this transport is that
CLIO never reads or writes the user's real ``~/.codex/auth.json``, so no test
here may depend on (or risk touching) it.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.lm_provider_types import LMProviderPreset
from clio_agent.gact.provider_catalog import discover_provider
from clio_agent.providers.claude_code_cancel import (
    _reset_for_tests as reset_cancel_registry,
)
from clio_agent.providers.claude_code_cancel import (
    abort_session_streams,
)
from clio_agent.providers.codex import sdk_client, sdk_discovery, sdk_transport
from clio_agent.providers.codex.constants import LITELLM_PROVIDER, LITELLM_PROVIDER_SDK


@pytest.fixture(autouse=True)
def _isolated_codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a test resolve the real ``~/.codex`` even by accident."""

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex_home"))
    monkeypatch.delenv("CLIO_MODEL_CATALOG", raising=False)
    monkeypatch.setattr(
        "clio_agent.providers.model_discovery.overlay.overlay_path",
        lambda: tmp_path / "model_catalog.json",
    )


def _preset() -> LMProviderPreset:
    return LMProviderPreset(
        id="codex",
        label="Codex",
        provider="codex",
        api_base="codex://direct",
        suggested_model="",
    )


# --------------------------------------------------------------------------- #
# CLIO never touches the user's auth.json (structural + behavioral).
# --------------------------------------------------------------------------- #


def _string_literals_excluding_docstrings(source: str) -> list[str]:
    """Every string literal in ``source`` EXCEPT module/function/class docstrings.

    Docstrings are free to explain, in prose, why ``auth.json`` is never
    touched (as this module's own do) -- what must never appear is the
    literal as an actual value the CODE constructs or compares against.
    """
    tree = ast.parse(source)
    docstring_constants: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                docstring_constants.add(id(node.body[0].value))
    literals: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstring_constants:
                literals.append(node.value)
    return literals


def test_sdk_modules_never_reference_auth_json() -> None:
    """The whole point of the restore: no code path builds an auth.json path.

    Docstrings (this module's included) may still EXPLAIN, in prose, that
    auth.json is never touched -- so this checks actual code string literals,
    not raw substring containment over the whole file.
    """

    for module in (sdk_client, sdk_discovery, sdk_transport):
        source = Path(inspect.getfile(module)).read_text(encoding="utf-8")
        literals = _string_literals_excluding_docstrings(source)
        offenders = [lit for lit in literals if "auth.json" in lit]
        assert offenders == [], (
            f"{module.__name__} builds a literal referencing auth.json: {offenders}"
        )


def test_codex_config_carries_no_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SDK client must never override ``env`` -- the subprocess inherits
    THIS process's real environment (the user's own CODEX_HOME), never a
    CLIO-managed copy of credentials."""

    captured: dict[str, Any] = {}

    class _FakeClient:
        def __init__(self, config: Any) -> None:
            captured["config"] = config

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *_a: object) -> bool:
            return False

    monkeypatch.setattr(sdk_client, "AsyncCodex", _FakeClient)
    client = sdk_client.CodexSDKClient()
    asyncio.run(client._ensure_client())

    config = captured["config"]
    assert config.env is None


# --------------------------------------------------------------------------- #
# SDK availability -- asked of the SDK itself, never a file check.
# --------------------------------------------------------------------------- #


class _FakeAccountResponse:
    def __init__(self, signed_in: bool) -> None:
        self.account = object() if signed_in else None


class _FakeModelsResponse:
    def __init__(self, rows: list[Any]) -> None:
        self.data = rows


def _fake_model_row(model_id: str, *, is_default: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        id=model_id,
        display_name=model_id,
        description="",
        is_default=is_default,
        # No ``model_fields_set`` -- reported_modalities() must treat this as
        # "unreported", never manufacture a capability nobody sent.
    )


class _FakeSdkClient:
    """Fakes the ``openai_codex.AsyncCodex`` boundary for discovery tests."""

    def __init__(self, *, signed_in: bool, rows: list[Any] | None = None) -> None:
        self._signed_in = signed_in
        self._rows = rows or []

    def __call__(self, *_args: object, **_kwargs: object) -> "_FakeSdkClient":
        return self

    async def __aenter__(self) -> "_FakeSdkClient":
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False

    async def account(self) -> _FakeAccountResponse:
        return _FakeAccountResponse(self._signed_in)

    async def models(self) -> _FakeModelsResponse:
        return _FakeModelsResponse(self._rows)


@pytest.mark.asyncio
async def test_sdk_discovery_installed_and_signed_in(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeSdkClient(
        signed_in=True,
        rows=[_fake_model_row("gpt-5.6-sol", is_default=True), _fake_model_row("gpt-5.6-mini")],
    )
    monkeypatch.setattr(sdk_discovery, "AsyncCodex", fake)

    result = await sdk_discovery.discover_codex_sdk_async()

    assert result.failed_reason is None
    assert {m["id"] for m in result.discovered} == {"gpt-5.6-sol", "gpt-5.6-mini"}
    assert result.default_model == "gpt-5.6-sol"
    # Never widened past what the row actually reported (#1211-style discipline).
    assert all(m["capabilities"] == [] for m in result.discovered)
    assert all(
        m["capability_evidence"]["reason"] == "modality_unreported" for m in result.discovered
    )


@pytest.mark.asyncio
async def test_sdk_discovery_installed_but_signed_out(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeSdkClient(signed_in=False)
    monkeypatch.setattr(sdk_discovery, "AsyncCodex", fake)

    result = await sdk_discovery.discover_codex_sdk_async()

    assert result.discovered == []
    assert result.failed_reason is not None
    assert result.failed_reason.startswith("codex_sdk_signed_out")


@pytest.mark.asyncio
async def test_sdk_discovery_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    # Simulate the package being unimportable without touching the real
    # install: setting a module to None makes Python raise ImportError.
    monkeypatch.setitem(sys.modules, "openai_codex", None)

    result = await sdk_discovery.discover_codex_sdk_async()

    assert result.discovered == []
    assert result.failed_reason is not None
    assert result.failed_reason.startswith("codex_sdk_not_installed")


@pytest.mark.asyncio
async def test_sdk_discovery_zero_models_is_typed_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeSdkClient(signed_in=True, rows=[])
    monkeypatch.setattr(sdk_discovery, "AsyncCodex", fake)

    result = await sdk_discovery.discover_codex_sdk_async()

    assert result.discovered == []
    assert result.failed_reason is not None
    assert result.failed_reason.startswith("codex_sdk_zero_models")


# --------------------------------------------------------------------------- #
# Catalog: both transports listed with their own health + models; the
# provider is READY when EITHER transport is (owner requirement).
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_catalog_lists_both_transports_with_their_own_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clio_agent.providers.handshake.model import (
        AuthState,
        ConnectivityState,
        HandshakeReport,
        ModelProfile,
    )

    preset = _preset()

    async def _handshake(*_a: object, **_kw: object) -> HandshakeReport:
        return HandshakeReport(
            provider_id="codex",
            provider_kind="codex",
            connectivity=ConnectivityState.OK,
            auth=AuthState.NOT_REQUIRED,
            models_source="overlay",
            models=(ModelProfile(id="gpt-5.5"),),
        )

    async def _refresh(**_kw: object) -> list[dict[str, object]]:
        return [{"provider": "codex", "failed_reason": ""}]

    monkeypatch.setattr(
        "clio_agent.gact.provider_catalog.model_discovery.overlay_models_wire",
        lambda *_a: {"models": [{"id": "gpt-5.5"}]},
    )
    monkeypatch.setattr("clio_agent.gact.provider_catalog.model_discovery.refresh_all", _refresh)
    monkeypatch.setattr("clio_agent.gact.provider_catalog.run_handshake", _handshake)

    fake_sdk = _FakeSdkClient(
        signed_in=True, rows=[_fake_model_row("gpt-5.6-sol", is_default=True)]
    )
    monkeypatch.setattr(sdk_discovery, "AsyncCodex", fake_sdk)

    provider = await discover_provider(preset, refresh=True)

    assert provider["health"] == "ready"
    transports = {t["id"]: t for t in provider["transports"]}
    assert set(transports) == {"sdk", "direct"}
    assert transports["sdk"]["health"] == "ready"
    assert [m["model_id"] for m in transports["sdk"]["models"]] == ["gpt-5.6-sol"]
    assert transports["direct"]["health"] == "ready"
    assert [m["model_id"] for m in transports["direct"]["models"]] == ["gpt-5.5"]
    # Every model on the merged, provider-level list carries its own transport.
    merged = {(m["model_id"], m["transport"]) for m in provider["models"]}
    assert merged == {("gpt-5.6-sol", "sdk"), ("gpt-5.5", "direct")}


@pytest.mark.asyncio
async def test_catalog_is_ready_when_sdk_available_even_if_direct_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owner requirement: green if ANY transport is available."""
    from clio_agent.providers.handshake.model import AuthState, ConnectivityState, HandshakeReport

    preset = _preset()

    async def _handshake(*_a: object, **_kw: object) -> HandshakeReport:
        return HandshakeReport(
            provider_id="codex",
            provider_kind="codex",
            connectivity=ConnectivityState.SKIPPED,
            auth=AuthState.MISSING,
            error="Codex sign-in is required on the connected agent",
        )

    async def _refresh(**_kw: object) -> list[dict[str, object]]:
        return [{"provider": "codex", "failed_reason": "Codex sign-in is required"}]

    monkeypatch.setattr(
        "clio_agent.gact.provider_catalog.model_discovery.overlay_models_wire",
        lambda *_a: None,
    )
    monkeypatch.setattr("clio_agent.gact.provider_catalog.model_discovery.refresh_all", _refresh)
    monkeypatch.setattr("clio_agent.gact.provider_catalog.run_handshake", _handshake)

    fake_sdk = _FakeSdkClient(signed_in=True, rows=[_fake_model_row("gpt-5.6-sol")])
    monkeypatch.setattr(sdk_discovery, "AsyncCodex", fake_sdk)

    provider = await discover_provider(preset, refresh=True)

    assert provider["health"] == "ready"
    assert provider["failure"] == ""
    transports = {t["id"]: t for t in provider["transports"]}
    assert transports["direct"]["health"] == "unavailable"
    assert transports["sdk"]["health"] == "ready"


# --------------------------------------------------------------------------- #
# Startup: the SDK transport must be checked at boot, not left "not checked"
# until someone clicks an explicit refresh (the owner's original complaint).
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_startup_probes_the_sdk_transport_and_a_fresh_catalog_read_reports_it_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh (passive) catalog read must show the SDK ready right after boot --
    never "codex_sdk_not_checked" -- and never trigger its OWN live probe (the
    startup task already recorded the result; a passive read only serves it)."""
    from clio_agent.providers.model_discovery import claude_code_catalog
    from clio_agent.providers.model_discovery import refresh as md_refresh

    # Isolate from the direct transport's own sign-in state entirely: the SDK
    # must be probed regardless of whether the direct credential is signed in.
    monkeypatch.setattr(md_refresh, "is_provider_configured", lambda _preset: False)
    monkeypatch.setattr(claude_code_catalog, "refresh_claude_code_candidates", lambda: [])

    fake_sdk = _FakeSdkClient(
        signed_in=True, rows=[_fake_model_row("gpt-5.6-luna", is_default=True)]
    )
    monkeypatch.setattr(sdk_discovery, "AsyncCodex", fake_sdk)

    await md_refresh.refresh_subscription_catalogs_at_startup()

    # The fresh read is passive (refresh=False): it must not re-probe the SDK
    # itself -- flip the fake so a live re-probe would prove itself by failing.
    monkeypatch.setattr(sdk_discovery, "AsyncCodex", _FakeSdkClient(signed_in=False))

    provider = await discover_provider(_preset(), refresh=False)

    sdk_transport_row = {t["id"]: t for t in provider["transports"]}["sdk"]
    assert sdk_transport_row["health"] == "ready"
    assert not sdk_transport_row["reason"].startswith("codex_sdk_not_checked")
    assert [m["model_id"] for m in sdk_transport_row["models"]] == ["gpt-5.6-luna"]


# --------------------------------------------------------------------------- #
# Model selection routes to the right LiteLLM provider (S1b end-to-end).
# --------------------------------------------------------------------------- #


def test_model_selection_routes_to_direct_by_default() -> None:
    from clio_agent.config import LMProviderConfig
    from clio_agent.lm.factory import _resolve_model_name

    config = LMProviderConfig(provider="codex", model="gpt-5.5", api_base="codex://direct")
    assert config.codex_variant == "direct"
    assert _resolve_model_name(config) == f"{LITELLM_PROVIDER}/cg-gpt-5.5"


def test_model_selection_routes_to_sdk_when_variant_selected() -> None:
    from clio_agent.config import LMProviderConfig
    from clio_agent.lm.factory import _resolve_model_name

    config = LMProviderConfig(
        provider="codex", model="gpt-5.5", api_base="codex://direct", codex_variant="sdk"
    )
    assert _resolve_model_name(config) == f"{LITELLM_PROVIDER_SDK}/cg-gpt-5.5"


def test_provider_registration_picks_the_bound_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.config import LMProviderConfig
    from clio_agent.lm import factory

    registered: list[str] = []
    monkeypatch.setattr(
        "clio_agent.providers.codex.litellm_adapter.ensure_registered",
        lambda: registered.append("direct"),
    )
    monkeypatch.setattr(
        "clio_agent.providers.codex.sdk_transport.ensure_registered",
        lambda: registered.append("sdk"),
    )

    direct_cfg = LMProviderConfig(provider="codex", model="gpt-5.5", api_base="codex://direct")
    factory._ensure_provider_registered(direct_cfg)
    sdk_cfg = LMProviderConfig(
        provider="codex", model="gpt-5.5", api_base="codex://direct", codex_variant="sdk"
    )
    factory._ensure_provider_registered(sdk_cfg)

    assert registered == ["direct", "sdk"]


def test_invalid_codex_variant_is_rejected() -> None:
    from clio_agent.config import LMProviderConfig

    with pytest.raises(ValueError, match="codex_variant"):
        LMProviderConfig(
            provider="codex",
            model="gpt-5.5",
            api_base="codex://direct",
            codex_variant="bogus",  # type: ignore[arg-type]
        )


# --------------------------------------------------------------------------- #
# The turn cancel contract (L1): a session cancel interrupts a real SDK turn.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cancel_interrupts_an_in_flight_sdk_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_cancel_registry()
    interrupted = asyncio.Event()

    class _FakeTurn:
        async def stream(self) -> Any:
            yield SimpleNamespace(
                method="item/agentMessage/delta", payload=SimpleNamespace(delta="hi")
            )
            # Blocks "indefinitely" -- bounded by this test's own wait_for calls,
            # simulating an in-flight turn that only a cancel ever ends.
            await asyncio.sleep(3600)

        async def interrupt(self) -> None:
            interrupted.set()

    class _FakeThread:
        async def turn(self, *_a: object, **_kw: object) -> _FakeTurn:
            return _FakeTurn()

    class _FakeClient:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *_a: object) -> bool:
            return False

        async def thread_start(self, **_kw: object) -> _FakeThread:
            return _FakeThread()

        async def close(self) -> None:
            pass

    monkeypatch.setattr(sdk_client, "AsyncCodex", _FakeClient)

    # An empty session id is the deliberate off-turn no-op
    # (register_sdk_stream/abort_session_streams both refuse it) -- a real
    # in-flight turn is always bound to a real GACT session, so bind one.
    from clio_agent.gact import context as gact_context

    session_token = gact_context.set_session_id("sess-codex-sdk-cancel-test")

    client = sdk_client.CodexSDKClient()
    try:
        agen = client.stream(
            prompt="hi", images=None, model="gpt-5.5", cwd=None, effort=None, timeout=30.0
        )
        first = await asyncio.wait_for(agen.__anext__(), timeout=10.0)
        assert first.method == "item/agentMessage/delta"

        # A real session cancel goes through the shared registry, never a
        # direct reference to the pump's future.
        cancelled = abort_session_streams("sess-codex-sdk-cancel-test")
        assert cancelled == 1

        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(agen.__anext__(), timeout=10.0)

        await asyncio.wait_for(interrupted.wait(), timeout=10.0)
    finally:
        gact_context.reset(session_token)
        client.close_blocking()
