"""Codex SDK transport discovery: installed + signed-in + live model list (S1b).

Per the owner ruling that restored this transport, availability is asked of
the SDK itself -- its own ``account()`` / ``models()`` RPCs against the
Codex runtime -- never by reading ``~/.codex/auth.json`` or any other file.
The SDK/runtime owns its own login state; this module only asks it questions.

Three typed outcomes distinguish exactly what an install/sign-in UI would need
to know (owner requirement: "why does Codex show as not available when I have
Codex installed and ready"):

* ``codex_sdk_not_installed`` -- the ``openai_codex`` package (or its bundled
  runtime binary) is not importable/launchable on this machine.
* ``codex_sdk_signed_out`` -- the SDK/runtime started fine, but its own
  ``account()`` RPC reports no signed-in account.
* ``codex_sdk_probe_failed`` -- the SDK started and (as far as we know) has an
  account, but the live probe itself failed (network, timeout, transport).

A successful probe reuses the maintained catalog's discovery *result* shape
(:class:`~clio_agent.providers.model_discovery.overlay.ProviderDiscoveryResult`)
so both codex transports' raw discovery output has one common contract.
"""

from __future__ import annotations

import asyncio
from typing import Any

from clio_agent.providers.model_discovery.modality_evidence import (
    modality_evidence,
    reported_modalities,
)
from clio_agent.providers.model_discovery.overlay import (
    CODEX_SOURCE,
    ProviderDiscoveryResult,
)

#: The modalities a Codex row can claim beyond text.
_CODEX_NON_TEXT_MODALITIES = ("image",)

#: Typed reasons, in the ``stream_fallback``/``PASSIVE_TOKEN_REASONS`` style:
#: the code is the queryable fact, the sentence is what a person reads.
SDK_UNAVAILABLE_REASONS: dict[str, str] = {
    "codex_sdk_not_installed": ("the Codex SDK/runtime is not installed on the connected agent"),
    "codex_sdk_signed_out": (
        "the Codex SDK/runtime is installed, but no account is signed in "
        "(sign in with the Codex CLI on the connected agent)"
    ),
    "codex_sdk_zero_models": "the Codex SDK reported zero available models",
    "codex_sdk_probe_failed": "the Codex SDK live probe failed",
}

#: Deliberate injection seam for focused discovery tests. The official SDK is
#: still imported only when discovery is requested; keeping the default as
#: ``None`` prevents provider startup from loading Codex for unrelated users.
AsyncCodex: Any | None = None

_DEFAULT_PROBE_TIMEOUT_S = 20.0


def _typed_failure(code: str, *, detail: str = "") -> str:
    message = f"{code}: {SDK_UNAVAILABLE_REASONS[code]}"
    return f"{message} ({detail})" if detail else message


def _codex_capability_row(row: Any) -> dict[str, Any]:
    """Return one discovered row's capabilities plus their typed evidence.

    The pinned ``openai_codex`` SDK declares ``Model.input_modalities`` with a
    schema default of ``["text", "image"]``, so reading the attribute directly
    would manufacture an image capability for any wire row that omitted the
    field. Capabilities are therefore stamped ONLY from ``model_fields_set``.
    """

    values = reported_modalities(row, "input_modalities")
    if values is None:
        return {
            "capabilities": [],
            "capability_evidence": modality_evidence(
                source="codex_sdk_input_modalities",
                reason="modality_unreported",
                unevidenced=_CODEX_NON_TEXT_MODALITIES,
            ),
        }
    return {
        "capabilities": values,
        "capability_evidence": modality_evidence(
            source="codex_sdk_input_modalities",
            reason="modality_reported",
        ),
    }


def _effort_value(effort: Any) -> str:
    return str(getattr(effort, "value", effort) or "")


def _codex_reasoning_row(row: Any) -> dict[str, Any]:
    """Return the reasoning efforts the SDK reports for one model (its own truth)."""

    options = getattr(row, "supported_reasoning_efforts", None) or []
    supported = [
        value
        for value in (_effort_value(getattr(option, "reasoning_effort", "")) for option in options)
        if value
    ]
    return {
        "supported_reasoning_efforts": supported,
        "default_reasoning_effort": _effort_value(getattr(row, "default_reasoning_effort", "")),
    }


async def _probe(timeout: float) -> ProviderDiscoveryResult:
    try:
        from openai_codex import AsyncCodex as SDKAsyncCodex  # noqa: PLC0415
        from openai_codex import CodexConfig, CodexError  # noqa: PLC0415

        from clio_agent.providers.codex.sdk_client import (  # noqa: PLC0415
            BARE_LM_CONFIG_OVERRIDES,
        )
    except ImportError as exc:
        return ProviderDiscoveryResult(
            provider="codex_sdk",
            discovered=[],
            source=CODEX_SOURCE,
            failed_reason=_typed_failure("codex_sdk_not_installed", detail=str(exc)),
        )

    sdk_client = AsyncCodex or SDKAsyncCodex

    async def _query() -> tuple[bool, Any]:
        # No ``env=`` override: this asks the SAME runtime/home the SDK
        # transport itself runs against (the user's own CODEX_HOME) --
        # never a CLIO-managed copy, and never a file read of auth.json.
        async with sdk_client(CodexConfig(config_overrides=BARE_LM_CONFIG_OVERRIDES)) as client:
            account = await client.account()
            if getattr(account, "account", None) is None:
                return False, None
            return True, await client.models()

    try:
        signed_in, response = await asyncio.wait_for(_query(), timeout=timeout)
    except ImportError as exc:
        # The runtime binary itself (not the python package) can be the piece
        # that's missing -- surfaces as an import error from inside the SDK's
        # own launch path on some platforms.
        return ProviderDiscoveryResult(
            provider="codex_sdk",
            discovered=[],
            source=CODEX_SOURCE,
            failed_reason=_typed_failure("codex_sdk_not_installed", detail=str(exc)),
        )
    except (OSError, CodexError, RuntimeError, TimeoutError) as exc:
        return ProviderDiscoveryResult(
            provider="codex_sdk",
            discovered=[],
            source=CODEX_SOURCE,
            failed_reason=_typed_failure("codex_sdk_probe_failed", detail=str(exc) or repr(exc)),
        )

    if not signed_in:
        return ProviderDiscoveryResult(
            provider="codex_sdk",
            discovered=[],
            source=CODEX_SOURCE,
            failed_reason=_typed_failure("codex_sdk_signed_out"),
        )

    rows = list(response.data)
    discovered = [
        {
            "id": str(row.id),
            "name": str(row.display_name or row.id),
            "description": str(row.description or ""),
            **_codex_capability_row(row),
            **_codex_reasoning_row(row),
        }
        for row in rows
        if row.id
    ]
    if not discovered:
        return ProviderDiscoveryResult(
            provider="codex_sdk",
            discovered=[],
            source=CODEX_SOURCE,
            failed_reason=_typed_failure("codex_sdk_zero_models"),
        )
    default_model = next((str(row.id) for row in rows if row.is_default), "")
    return ProviderDiscoveryResult(
        provider="codex_sdk",
        discovered=discovered,
        source=CODEX_SOURCE,
        default_model=default_model,
    )


def discover_codex_sdk(*, timeout: float = _DEFAULT_PROBE_TIMEOUT_S) -> ProviderDiscoveryResult:
    """Refresh the account's live Codex model list through the SDK transport.

    The Python SDK owns its pinned runtime and authentication; CLIO neither
    resolves a ``codex`` executable nor opens an app-server protocol
    connection itself, and never touches ``~/.codex/auth.json``.
    """

    try:
        return asyncio.run(_probe(timeout))
    except RuntimeError as exc:
        # asyncio.run() refuses when a loop is already running on this thread
        # (callers on the gact event loop use the async probe directly).
        return ProviderDiscoveryResult(
            provider="codex_sdk",
            discovered=[],
            source=CODEX_SOURCE,
            failed_reason=_typed_failure("codex_sdk_probe_failed", detail=str(exc)),
        )


async def discover_codex_sdk_async(
    *, timeout: float = _DEFAULT_PROBE_TIMEOUT_S
) -> ProviderDiscoveryResult:
    """Async form of :func:`discover_codex_sdk` for callers already on an event loop."""

    return await _probe(timeout)


__all__ = [
    "SDK_UNAVAILABLE_REASONS",
    "discover_codex_sdk",
    "discover_codex_sdk_async",
]
