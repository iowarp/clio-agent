"""Health-derivation fixes for the normalized provider catalog (#1446 follow-up).

A live-verified bug: ``argonne_metis`` reported ``health: "ready"`` with
``connectivity: "ok"``, ``auth: "ok"``, ``models: []`` and an EMPTY
``failure`` string, because the old check was ``report.ok`` alone -- it never
looked at whether any model actually came back, and the underlying exception
(``globus-sdk`` missing) had an empty ``str()`` that vanished into nothing.
These tests pin down the fix: READY requires real evidence (ok AND at least
one model); a missing optional dependency is a typed ``needs_install`` state,
never a generic failure; and no exception's empty ``str()`` can produce an
empty reason anywhere in the chain.
"""

from __future__ import annotations

import asyncio

import pytest

from clio_agent.gact.provider_catalog import (
    NEEDS_INSTALL_ERROR_CODES,
    _resolve_health,
    discover_provider,
)
from clio_agent.gact.types import LMProviderPreset
from clio_agent.providers.handshake.base import (
    ConnectivityResult,
    HandshakeContext,
    ProviderHandshake,
    describe_exception,
)
from clio_agent.providers.handshake.model import AuthState, ConnectivityState, DiscoveredModel


def _preset(provider: str = "argonne_metis", provider_kind: str = "argonne") -> LMProviderPreset:
    return LMProviderPreset(
        id=provider,
        label="Metis",
        provider=provider_kind,
        api_base="https://inference-api.alcf.anl.gov/resource_server/metis/vllm/v1",
        suggested_model="",
        requires_api_key=False,
    )


# --------------------------------------------------------------------------- #
# describe_exception -- never an empty reason.
# --------------------------------------------------------------------------- #


def test_describe_exception_falls_back_to_type_and_repr_when_str_is_empty() -> None:
    class _SilentError(RuntimeError):
        pass

    exc = _SilentError()
    assert str(exc) == ""  # the premise: this exception really has nothing to say

    described = describe_exception(exc)

    assert described != ""
    assert "_SilentError" in described


def test_describe_exception_keeps_a_real_message_unchanged() -> None:
    exc = RuntimeError("connection refused")
    assert describe_exception(exc) == "connection refused"


# --------------------------------------------------------------------------- #
# _resolve_health -- the actual bug: ok + zero models + no failure was "ready".
# --------------------------------------------------------------------------- #


def test_resolve_health_zero_models_claimed_success_is_never_ready() -> None:
    health, failure = _resolve_health(ok=True, models=(), error="", error_code="")

    assert health == "unavailable"
    assert failure != ""  # never a silently empty reason


def test_resolve_health_ready_requires_both_ok_and_models() -> None:
    health, failure = _resolve_health(
        ok=True, models=(DiscoveredModel(id="m"),), error="", error_code=""
    )
    assert (health, failure) == ("ready", "")


def test_resolve_health_recorded_failure_is_never_ready_even_with_models() -> None:
    # A cached/last-good list can be non-empty on a report that still carries
    # a live failure; the failure must win.
    health, failure = _resolve_health(
        ok=True, models=(DiscoveredModel(id="m"),), error="upstream 500", error_code=""
    )
    assert health == "unavailable"
    assert failure == "upstream 500"


def test_resolve_health_missing_optional_dependency_is_needs_install() -> None:
    assert "argonne_sdk_missing" in NEEDS_INSTALL_ERROR_CODES

    health, failure = _resolve_health(
        ok=False,
        models=(),
        error="argonne_sdk_missing: the 'argonne' extra (globus-sdk) is not installed",
        error_code="argonne_sdk_missing",
    )

    assert health == "needs_install"
    assert failure.startswith("argonne_sdk_missing")


# --------------------------------------------------------------------------- #
# The exact reported bug, end to end through discover_provider.
# --------------------------------------------------------------------------- #


def test_argonne_zero_models_with_empty_error_is_unavailable_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reproduces the live ``argonne_metis`` observation verbatim."""

    preset = _preset()

    async def _handshake(*_a: object, **_kw: object):
        # connectivity ok / auth ok / zero models / EMPTY error -- exactly
        # what the old ``report.ok`` check let through as "ready".
        from clio_agent.providers.handshake.model import HandshakeReport

        return HandshakeReport(
            provider_id=preset.id,
            provider_kind=preset.provider,
            connectivity=ConnectivityState.OK,
            auth=AuthState.OK,
            models_source="unavailable",
            models=(),
            error=None,
        )

    monkeypatch.setattr("clio_agent.gact.provider_catalog.run_handshake", _handshake)

    provider = asyncio.run(discover_provider(preset))

    assert provider["health"] != "ready"
    assert provider["health"] == "unavailable"
    assert provider["failure"] != ""


def test_argonne_missing_sdk_reports_needs_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing 'argonne' extra is a typed install state, not a bare failure."""

    preset = _preset()

    class _FakeArgonneHandshake:
        def __init__(self, provider: object) -> None:
            self.provider = provider

        async def handshake(self, ctx: HandshakeContext):
            from clio_agent.providers.handshake.model import HandshakeReport

            return HandshakeReport(
                provider_id=ctx.provider_id,
                provider_kind=ctx.provider_kind,
                connectivity=ConnectivityState.SKIPPED,
                auth=AuthState.MISSING,
                error=(
                    "argonne_sdk_missing: the 'argonne' extra (globus-sdk) is not "
                    "installed, so the stored Globus sign-in cannot be used"
                ),
                error_code="argonne_sdk_missing",
            )

    async def _run_handshake(ctx: HandshakeContext, *, force: bool = False):
        return await _FakeArgonneHandshake(None).handshake(ctx)

    monkeypatch.setattr("clio_agent.gact.provider_catalog.run_handshake", _run_handshake)

    provider = asyncio.run(discover_provider(preset))

    assert provider["health"] == "needs_install"
    assert provider["failure"].startswith("argonne_sdk_missing")


# --------------------------------------------------------------------------- #
# The handshake phase template itself never lets a discover_models exception
# with an empty str() produce an empty HandshakeReport.error.
# --------------------------------------------------------------------------- #


class _EmptyMessageError(RuntimeError):
    """An exception that genuinely has nothing in ``str()`` (like a bare
    ``ImportError()`` from a missing optional dependency)."""


class _RaisingDiscoveryHandshake(ProviderHandshake):
    async def check_connectivity(self, client: object, ctx: HandshakeContext) -> ConnectivityResult:
        return ConnectivityResult(connectivity=ConnectivityState.OK, auth=AuthState.OK)

    async def discover_models(self, client: object, ctx: HandshakeContext) -> list[dict]:
        raise _EmptyMessageError()

    async def discover_model_config(self, client: object, ctx: HandshakeContext, raw: dict):
        raise AssertionError("never reached: discover_models raised first")


@pytest.mark.asyncio
async def test_handshake_model_discovery_failure_with_empty_str_is_not_silently_empty() -> None:
    handshake = _RaisingDiscoveryHandshake(provider=None)
    ctx = HandshakeContext(
        provider_id="p", provider_kind="k", api_base="http://example.invalid", auth_mode="passive"
    )

    report = await handshake.handshake(ctx)

    assert report.ok is True  # connectivity/auth succeeded; discovery is what failed
    assert report.models == ()
    assert report.error is not None
    assert report.error != "model discovery failed: "
    assert "_EmptyMessageError" in report.error
