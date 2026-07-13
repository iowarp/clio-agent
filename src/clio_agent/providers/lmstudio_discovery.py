"""LM Studio model discovery (CLI-facing) via the provider handshake.

Extracted from :mod:`clio_agent.config` (#769). ``clio_agent.config`` re-exports
these names so historical import seams keep working; new code should import from
:mod:`clio_agent.providers.lmstudio_discovery` directly.
"""

from __future__ import annotations

from typing import List


class LMStudioDiscoveryError(RuntimeError):
    """LM Studio model discovery failed before a usable chat model was found."""


def list_lm_studio_models(
    base_url: str = "http://127.0.0.1:1234", max_retries: int = 10, retry_delay: float = 2.0
) -> List[str]:
    """Discover loaded LM Studio model IDs through the unified provider handshake.

    This is the single LM Studio discovery path: the CLI (here) and the gact
    server both go through :class:`LMStudioHandshake`, so there is no longer a
    second, divergent probe to rot. The former standalone HTTP fetch is gone;
    its one genuinely-CLI-specific behaviour — *retry while LM Studio is still
    loading a model* — is preserved here: a reachable-but-empty result is
    retried up to ``max_retries`` times. A persistently unreachable backend, or
    one that never reports a loaded model, raises :class:`LMStudioDiscoveryError`
    with actionable text, exactly as before.

    Args:
        base_url: LM Studio base URL (with or without a ``/v1`` suffix).
        max_retries: Maximum probe attempts while waiting for a model to load.
        retry_delay: Delay between attempts in seconds.

    Returns:
        List of loaded model IDs.
    """
    import time

    from clio_agent.providers.handshake import HandshakeContext, run_handshake_sync

    last_error: str | None = None
    for attempt in range(max_retries):
        report = run_handshake_sync(
            HandshakeContext(
                provider_id="lm_studio",
                provider_kind="lm_studio",
                api_base=base_url,
                auth_mode="passive",
                # Names only: the context cascade (models.dev/db) isn't needed to
                # pick a model, and skipping it keeps discovery offline-fast.
                allow_external_sources=False,
            ),
            # Bypass the handshake TTL cache so each retry re-probes a backend
            # that may still be loading its first model.
            force=True,
        )
        if report.ok and report.models:
            return [m.id for m in report.models if m.id]
        last_error = report.error or "no loaded models reported"
        if attempt == 0:
            print(f"Connecting to LM Studio at {base_url}...")
        print(f"   Waiting for a loaded model... (attempt {attempt + 1}/{max_retries})")
        if attempt < max_retries - 1:
            time.sleep(retry_delay)

    raise LMStudioDiscoveryError(
        f"LM Studio discovery failed at {base_url} after {max_retries} attempt(s): "
        f"{last_error}. Start LM Studio, load a chat/instruct model, or set "
        "CLIO_LM_API_BASE / CLIO_LM_MODEL."
    )


def _openai_compatible_api_base(base_url: str) -> str:
    """Return an OpenAI-compatible API base with exactly one /v1 suffix."""
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1"):
        return normalized
    return f"{normalized}/v1"


def select_models_for_agents(models: List[str]) -> tuple[str, str]:
    """Select main and expert models from available models.

    Prioritizes instruction-tuned/chat models and avoids embedding models.

    Args:
        models: List of available model IDs

    Returns:
        Tuple of (main_model, expert_model)

    Raises:
        ValueError: If discovery returned no usable chat/instruct model.
    """
    if not models:
        raise ValueError(
            "LM Studio reported no loaded models. Load a chat/instruct model, "
            "set CLIO_LM_MODEL explicitly, or reconfigure CLIO_LM_PROVIDER."
        )

    # Filter out embedding models
    chat_models = [m for m in models if "embedding" not in m.lower()]

    if not chat_models:
        raise ValueError(
            "LM Studio reported only embedding/non-chat models. Load a chat/instruct "
            f"model or set CLIO_LM_MODEL explicitly. Models: {', '.join(models)}"
        )

    # Strategy 1: Look for granite chat models
    granite_models = [m for m in chat_models if "granite" in m.lower()]

    if granite_models:
        main_model = granite_models[0]
        # Try to find a different granite model for expert, or use the same one
        if len(granite_models) > 1:
            expert_model = granite_models[1]
        else:
            expert_model = main_model
    else:
        # Strategy 2: If no granite models, take any available chat model.
        main_model = chat_models[0]
        # Try to pick a different model if possible
        remaining_models = [m for m in chat_models if m != main_model]
        if remaining_models:
            expert_model = remaining_models[0]
        else:
            expert_model = main_model

    print("Selected models:")
    print(f"  Main/Router: {main_model}")
    print(f"  Expert/Reasoner: {expert_model}")

    return main_model, expert_model
