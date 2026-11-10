"""GitHub Copilot model discovery: the signed-in account's LIVE ``list_models()``.

The models come from the Copilot runtime itself, asked with CLIO's own GitHub
sign-in (:mod:`clio_agent.providers.copilot.auth`). The runtime's
``auth.getStatus`` is read first, so a token the service refuses becomes the
typed sign-in error rather than a transport failure. There is no compiled-in or
maintained model list behind it: a model the account cannot use never appears,
and a new one appears as soon as the runtime lists it.

Rows whose ``policy.state`` is not ``enabled`` (an org or user policy turned
them off, or never configured them) are not offered; each one is recorded in
``rejected`` with a typed code, so a filtered model is visible, never silently
missing.

Each row carries only what the runtime reported: limits from
``capabilities.limits``, image input from ``capabilities.supports.vision``,
the reasoning efforts, and ``billing.multiplier`` (premium requests per call).
PDF input is not reported by the list and stays unevidenced.
"""

from __future__ import annotations

import asyncio
from typing import Any

from clio_agent.providers.copilot.errors import (
    COPILOT_SIGNED_OUT_MESSAGE,
    CopilotAuthRequiredError,
    CopilotError,
    teardown_errors,
)
from clio_agent.providers.model_discovery.modality_evidence import modality_evidence
from clio_agent.providers.model_discovery.overlay import (
    COPILOT_SOURCE,
    ProviderDiscoveryResult,
)
from clio_agent.runtime.fresh_loop import run_in_new_loop

#: Typed codes for a listed model CLIO does not offer.
POLICY_REJECTION_CODES: dict[str, str] = {
    "disabled": "copilot_policy_disabled",
    "unconfigured": "copilot_policy_unconfigured",
}

#: Bound on one discovery read (runtime start + ``models.list``).
LIST_MODELS_TIMEOUT_S = 60.0

#: ``failed_reason`` when the account answers but no listed model is enabled.
NO_ENABLED_MODELS_MESSAGE = "The signed-in GitHub account lists no enabled Copilot models."


async def _list_models() -> list[Any]:
    """Start a short-lived runtime under CLIO's GitHub token and read its model list."""
    from clio_agent.providers.copilot.auth import github_token  # noqa: PLC0415
    from clio_agent.providers.copilot.options import (  # noqa: PLC0415
        client_kwargs,
        copilot_home,
    )
    from clio_agent.providers.copilot.runtime import runtime_path  # noqa: PLC0415
    from clio_agent.providers.copilot.sessions import require_copilot_sdk  # noqa: PLC0415

    copilot = require_copilot_sdk()
    kwargs = client_kwargs(
        github_token=github_token(),
        runtime_path=runtime_path(),
        working_directory=str(copilot_home()),
    )
    client = copilot.CopilotClient(**kwargs)
    await client.start()
    try:
        status = await client.get_auth_status()
        if not status.isAuthenticated:
            raise CopilotAuthRequiredError(
                COPILOT_SIGNED_OUT_MESSAGE, detail=str(status.statusMessage or "")
            )
        return list(await client.list_models())
    finally:
        await client.stop()


def _limits(model: Any) -> tuple[int | None, int | None, str]:
    """``(context_window, output_limit, field)`` from ``capabilities.limits``.

    The prompt budget the service enforces (``max_prompt_tokens``) is the window
    CLIO compiles context into; the total window is used only when the prompt
    budget is not reported, and ``field`` records which one it was.
    """
    limits = getattr(getattr(model, "capabilities", None), "limits", None)
    output = getattr(limits, "max_output_tokens", None)
    prompt = getattr(limits, "max_prompt_tokens", None)
    if prompt:
        return int(prompt), int(output) if output else None, "max_prompt_tokens"
    total = getattr(limits, "max_context_window_tokens", None)
    if total:
        return int(total), int(output) if output else None, "max_context_window_tokens"
    return None, int(output) if output else None, ""


def _capability_row(model: Any) -> dict[str, Any]:
    supports = getattr(getattr(model, "capabilities", None), "supports", None)
    vision = bool(getattr(supports, "vision", False))
    return {
        "capabilities": ["text", "image"] if vision else ["text"],
        "capability_evidence": modality_evidence(
            source="copilot_list_models",
            reason="modality_reported",
            unevidenced=("pdf",) if vision else ("image", "pdf"),
        ),
    }


def discovered_row(model: Any) -> dict[str, Any]:
    """One overlay row for an enabled ``ModelInfo``."""
    context_window, output_limit, limit_field = _limits(model)
    supports = getattr(getattr(model, "capabilities", None), "supports", None)
    billing = getattr(model, "billing", None)
    multiplier = getattr(billing, "multiplier", None)
    return {
        "id": str(model.id),
        "name": str(model.name or model.id),
        "description": "",
        "context_window": context_window,
        "output_limit": output_limit,
        "context_source": COPILOT_SOURCE,
        "context_window_field": limit_field,
        **_capability_row(model),
        "reasoning_effort_supported": bool(getattr(supports, "reasoning_effort", False)),
        "supported_reasoning_efforts": list(model.supported_reasoning_efforts or []),
        "default_reasoning_effort": str(model.default_reasoning_effort or ""),
        "billing_multiplier": float(multiplier) if multiplier is not None else None,
    }


def _policy_state(model: Any) -> str:
    """The row's policy state; a model with no policy block is usable as listed."""
    policy = getattr(model, "policy", None)
    return "enabled" if policy is None else str(policy.state)


def rows_from_models(models: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Split a ``list_models()`` answer into offered rows and typed rejections."""
    offered: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    for model in models:
        state = _policy_state(model)
        if state == "enabled":
            offered.append(discovered_row(model))
            continue
        rejected.append(
            {
                "id": str(model.id),
                "code": POLICY_REJECTION_CODES.get(state, "copilot_policy_" + state),
                "reason": f"{model.name or model.id} is {state} by the GitHub Copilot policy "
                "of the signed-in account.",
            }
        )
    return offered, rejected


def discover_copilot() -> ProviderDiscoveryResult:
    """Read the signed-in account's Copilot models (blocking; run off the event loop).

    Returns:
        A result whose ``failed_reason`` is CLIO's typed error text (signed out,
        runtime missing, refused) when the list could not be read, and whose
        default is the first enabled model the account reports.
    """
    try:
        models = run_in_new_loop(asyncio.wait_for(_list_models(), LIST_MODELS_TIMEOUT_S))
    except CopilotError as exc:
        return ProviderDiscoveryResult(
            provider="copilot", discovered=[], source=COPILOT_SOURCE, failed_reason=str(exc)
        )
    except teardown_errors() as exc:
        return ProviderDiscoveryResult(
            provider="copilot",
            discovered=[],
            source=COPILOT_SOURCE,
            failed_reason=f"GitHub Copilot model list unavailable: {type(exc).__name__}: {exc}",
        )
    offered, rejected = rows_from_models(models)
    if not offered:
        return ProviderDiscoveryResult(
            provider="copilot",
            discovered=[],
            source=COPILOT_SOURCE,
            failed_reason=NO_ENABLED_MODELS_MESSAGE,
            rejected=rejected,
        )
    return ProviderDiscoveryResult(
        provider="copilot",
        discovered=offered,
        source=COPILOT_SOURCE,
        default_model=offered[0]["id"],
        rejected=rejected,
    )


__all__ = [
    "LIST_MODELS_TIMEOUT_S",
    "NO_ENABLED_MODELS_MESSAGE",
    "POLICY_REJECTION_CODES",
    "discover_copilot",
    "discovered_row",
    "rows_from_models",
]
