"""LM-provider catalog + runtime-bind routes for the GACT server (#714).

This concern owns the eight ``/v1/providers*`` routes the TUI's settings picker
drives:

* ``GET /v1/providers`` + ``GET /v1/providers/{provider_id}`` (SPEC §6.12) -- the
  generic provider catalog (one row per preset) and the per-provider detail row.
* ``POST /v1/providers/{provider_id}/auth`` -- the generic sign-in API
  (start/complete/status/logout; ALCF and Codex today, 405 hint otherwise).
* ``GET /v1/providers/{provider_id}/models`` + ``.../handshake`` -- the per-provider
  model catalog and connectivity/auth/per-model handshake via the unified async
  handshake (passive auth -- browsing never triggers interactive OAuth).
* ``GET /v1/providers/lm`` + ``PUT /v1/providers/lm`` + ``GET /v1/providers/lm/wait``
  report the live LM config, reconfigure it in-place (the
  async ``idle -> configuring -> ready/error`` bind), and block until the bind
  settles.

The write-side bind (``PUT /v1/providers/lm`` -> :func:`_apply_lm_provider`) is a
demoted **default-profile** action (design ``docs/archive/per-expert-provider-lm.md``
§5): it resolves the request to a config, folds its (cached) handshake, atomically
swaps the default entry of the per-app
:class:`~clio_agent.gact.providers.profile_store.ProviderProfileStore` (an immutable
RCU pointer swap), and rebuilds only the singleton main agent's LMs. It no longer
mutates ``os.environ`` or dspy ``main_thread_config``: experts select their LM
per-call via ``dspy.context`` and the boot ``dspy.configure`` default stays a
harmless fallback. With no shared mutable global left, there is no critical section
to serialize -- concurrent default binds do a last-writer-wins atomic snapshot swap.

The module imports only leaf packages -- the read-only provider helpers in
:mod:`clio_agent.gact.providers` (config/auth/lmstudio), the ARC accessors in
:mod:`clio_agent.gact.runtime.globals`, events, types and stdlib -- and never
loads :mod:`clio_agent.gact.app`. The two cross-concern ``build_app`` helpers the
bind needs (``install_tool_runtime_hooks`` / ``clear_session_model_refs``) travel
on :class:`~clio_agent.gact.routes.deps.GactDeps`.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException

from clio_agent.gact.agent_initialization import mark_agent_ready
from clio_agent.gact.events import Event
from clio_agent.gact.lm_provider_types import preset_api_key_env
from clio_agent.gact.providers.auth import (
    _is_placeholder_api_key,
    _resolve_argonne_runtime_api_key,
)
from clio_agent.gact.providers.config import (
    _default_profile_spec,
    _effective_lm_config,
    requested_thinking_level,
    thinking_level_record,
)
from clio_agent.gact.providers.lmstudio import (
    _lm_studio_api_root,
    _lm_studio_headers,
    _release_owned_lm_studio_instance,
)
from clio_agent.gact.providers.request_normalization import normalize_lm_provider_request
from clio_agent.gact.relay_wiring import construct_agent_with_relay
from clio_agent.gact.routes.codex_variant import apply_codex_readiness_gate
from clio_agent.gact.routes.provider_auth import supports_logout
from clio_agent.gact.routes.provider_catalog_routes import register_provider_catalog_routes
from clio_agent.gact.runtime.globals import _process_arc, _set_app_arc
from clio_agent.gact.types import (
    ErrorEnvelope,
    ErrorInfo,
    LMProviderInfo,
    LMProviderPreset,
    LMProviderRequest,
)

if TYPE_CHECKING:
    from clio_agent.gact.routes.deps import GactDeps


def _lmstudio_flash_attention_enabled() -> bool:
    """Whether LM Studio model loads request flash attention (default on).

    Flash attention drastically cuts KV-cache memory; without it a large-context
    load can wedge LM Studio mid-run (see the load-config comment in the bind
    route). Opt out via ``lm.lmstudio_flash_attention`` /
    ``CLIO_LMSTUDIO_FLASH_ATTENTION=0``.
    """
    from clio_agent import conf  # noqa: PLC0415 - avoid import cycle at module load

    return conf.resolve(
        "lm.lmstudio_flash_attention",
        env="CLIO_LMSTUDIO_FLASH_ATTENTION",
        default=True,
        cast=conf.as_bool,
    )


def register_providers_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register the ``/v1/providers*`` catalog + LM-bind routes on ``app``.

    Handlers close over the ``app`` argument (FastAPI's decorators need it) and
    reach the live agent / LM config status / event bus through ``app.state``. The
    preset + model catalogs are read from :mod:`clio_agent.providers.catalog` once
    at registration time (mirroring the original ``build_app`` behavior); the bind
    reaches the agent-rebuild hooks through ``deps``.
    """

    # ---- /v1/providers (#15) ------------------------------------------

    # Derived from clio_agent.providers.catalog. Add new presets to
    # the catalog, not here -- this list reflects whatever the catalog
    # contains at registration time. Polaris preset removed for the time
    # being -- the inference-api gateway returns 400 'cluster polaris
    # does not exist' for /resource_server/polaris/vllm/v1.
    from clio_agent.providers.catalog import as_lm_presets as _build_lm_presets

    _LM_PRESETS: list[LMProviderPreset] = _build_lm_presets()

    # Per-provider model catalogs. Hand-curated rather than introspected
    # because most upstreams either don't expose a /models endpoint or
    # return hundreds of irrelevant entries. The TUI's Settings → Model
    # picker calls this once per provider and lists the rows verbatim.
    # Derived from clio_agent.providers.catalog. Static fallback used
    # only when live model discovery against the upstream /v1/models
    # endpoint fails (no key, network down, 5xx) -- see the GET
    # /v1/providers/{id}/models handler below for the resolution order.
    # ALCF / Argonne live model availability is dynamic (jobs spin up
    # and tear down behind the gateway); the live set can be queried
    # with `scripts/list_active_models.sh` in alcf-agentics-workflow.
    from clio_agent.providers.catalog import (
        as_provider_models_dict as _build_provider_models,
    )

    _PROVIDER_MODELS: dict[str, list[dict[str, str]]] = _build_provider_models()

    def _codex_readiness(*, ignore_startup: bool = False) -> tuple[str, str, bool, str]:
        """Return status, message, verified flag, and live default for Codex."""

        from clio_agent.providers.codex.credentials import CodexCredentialStore  # noqa: PLC0415
        from clio_agent.providers.codex.errors import (  # noqa: PLC0415
            CODEX_AUTHENTICATION_ERROR_MESSAGE,
        )

        if not CodexCredentialStore().is_signed_in():
            return (
                "auth_required",
                CODEX_AUTHENTICATION_ERROR_MESSAGE,
                False,
                "",
            )
        startup_check = getattr(app.state, "provider_catalog_startup_task", None)
        if not ignore_startup and startup_check is not None and not startup_check.done():
            return "auth_check_required", "Codex models are being checked", False, ""
        from clio_agent.providers import model_discovery  # noqa: PLC0415

        try:
            overlay = model_discovery.overlay_models_wire("codex", "codex")
        except model_discovery.OverlayMalformedError as exc:
            return "unavailable", f"Codex model catalog is invalid: {exc}", False, ""
        if overlay and overlay.get("models") and not overlay.get("staleness"):
            return (
                "ready",
                "Codex credentials validated",
                True,
                str(overlay.get("default_model") or ""),
            )
        return (
            "auth_check_required",
            "Codex credentials are present but have not been validated",
            False,
            "",
        )

    def _claude_code_readiness(*, ignore_startup: bool = False) -> tuple[str, str, bool, str]:
        """Return status, message, verified flag, and live default for Claude Code."""

        from clio_agent.providers.claude_code_errors import (  # noqa: PLC0415
            CLAUDE_CODE_NOT_INSTALLED_MESSAGE,
        )

        if importlib.util.find_spec("claude_agent_sdk") is None:
            return "install_required", CLAUDE_CODE_NOT_INSTALLED_MESSAGE, False, ""
        startup_check = getattr(app.state, "provider_catalog_startup_task", None)
        if not ignore_startup and startup_check is not None and not startup_check.done():
            return "auth_check_required", "Claude Code models are being checked", False, ""
        from clio_agent.providers import model_discovery  # noqa: PLC0415

        try:
            overlay = model_discovery.overlay_models_wire("claude_code", "claude_code")
        except model_discovery.OverlayMalformedError as exc:
            return "unavailable", f"Claude Code model catalog is invalid: {exc}", False, ""
        if overlay and overlay.get("models") and not overlay.get("staleness"):
            return (
                "ready",
                "Claude Code sign-in and models verified",
                True,
                str(overlay.get("default_model") or ""),
            )
        return (
            "auth_check_required",
            "Claude Code is installed but has not been verified. Check the provider to sign in.",
            False,
            "",
        )

    def _provider_auth_state(preset: "LMProviderPreset") -> tuple[list[str], bool]:
        """Return (auth_methods, is_authenticated) for a preset.

        Maps CLIO's preset flags to the GACT v0.1 §6.12 Provider shape so
        the TUI's settings picker can render the right state badge:

        - argonne_*: globus oauth; authenticated when tokens are on disk
          AND globus-sdk is importable.
        - cloud (requires_api_key=True): api_key auth; authenticated when
          the matching env var is set.
        - codex: a signed-in credential must exist and have a fresh successful
          catalog check.
        - local (lm_studio/ollama): no auth required.
        """
        if preset.provider == "codex":
            _, _, verified, _ = _codex_readiness()
            return ["subscription"], verified
        if preset.provider == "claude_code":
            _, _, verified, _ = _claude_code_readiness()
            return ["subscription"], verified
        if preset.provider == "argonne":
            authed = False
            try:
                from clio_agent.providers import argonne_auth  # noqa: PLC0415

                authed = (
                    argonne_auth.tokens_exist()
                    and argonne_auth.sdk_available()
                    and argonne_auth.check_auth_status()
                )
            except Exception:  # noqa: BLE001 - auth probe failure treated as not-authed
                authed = False
            return ["oauth"], authed

        if preset.requires_api_key:
            # The preset's own env var (never a kind-keyed table -- openrouter/
            # nvidia_nim share kind "openai" with the literal OpenAI provider
            # and must not report "authenticated" off OPENAI_API_KEY, Part 3).
            env_var = preset.api_key_env or "CLIO_LM_API_KEY"
            return ["api_key"], bool(os.environ.get(env_var) or os.environ.get("CLIO_LM_API_KEY"))

        return ["none"], True

    def _default_model_for(preset: "LMProviderPreset") -> str:
        """The provider's suggested default model: overlay-discovered first (#1211
        review D2), the frozen static ``suggested_model`` otherwise. Once a
        refresh has run, this follows the CLI's/account's OWN live default
        (e.g. codex's ``gpt-5.6-sol``) instead of a snapshot that may already be
        rejected (#1184). CLI-provider candidates are never automatic defaults;
        only a fresh account discovery supplies one."""
        if preset.provider in {"codex", "claude_code"}:
            readiness = _codex_readiness if preset.provider == "codex" else _claude_code_readiness
            _, _, verified, default_model = readiness()
            return default_model if verified else ""

        from clio_agent.providers import model_discovery  # noqa: PLC0415

        overlay_default = model_discovery.overlay_default_model(preset.id, preset.provider)
        return overlay_default or preset.suggested_model

    def _provider_to_wire(preset: "LMProviderPreset") -> dict[str, Any]:
        auth_methods, is_authed = _provider_auth_state(preset)
        return {
            "id": preset.id,
            "name": preset.label,
            "auth_methods": auth_methods,
            "is_authenticated": is_authed,
            "default_model": _default_model_for(preset),
            "api_base": preset.api_base,
            "env_keys": (["CLIO_LM_API_KEY"] if preset.requires_api_key else []),
            "description": preset.description,
            "metadata": {
                "provider_kind": preset.provider,
                "requires_api_key": preset.requires_api_key,
                "supports_vision": bool(getattr(preset, "supports_vision", False)),
            },
        }

    @app.get("/v1/providers")
    async def list_providers() -> dict[str, Any]:
        """SPEC §6.12 — generic LM provider catalog.

        Returns one row per preset with the v0.1 fields (id, name,
        auth_methods, is_authenticated, default_model) so the TUI's
        settings picker can render the right state badge per provider
        and decide whether to surface a "Login" affordance.
        """

        return {"providers": [_provider_to_wire(p) for p in _LM_PRESETS]}

    # GET /v1/providers/{provider_id} is registered after the literal
    # /v1/providers/lm route so the LM configuration endpoint keeps
    # winning FastAPI's order-based route match.

    register_provider_catalog_routes(
        app, _LM_PRESETS, _PROVIDER_MODELS, _codex_readiness, _claude_code_readiness
    )

    # ---- /v1/providers/lm ------------------------

    def _preset_with_status(preset: LMProviderPreset) -> LMProviderPreset:
        update: dict[str, Any] = {"supports_logout": supports_logout(preset.provider)}
        if preset.provider == "argonne":
            try:
                from clio_agent.providers import argonne_auth  # noqa: PLC0415

                status, message, authed = argonne_auth.readiness()
            except Exception as exc:  # noqa: BLE001 - argonne unavailability surfaced in status/status_message
                status, message, authed = "unavailable", f"argonne auth unavailable: {exc}", False
            update["status"] = status
            update["status_message"] = message
            update["is_authenticated"] = authed
            return preset.model_copy(update=update)
        if preset.requires_api_key:
            env_key = preset_api_key_env(preset)
            if not (os.environ.get(env_key) or os.environ.get("CLIO_LM_API_KEY")):
                update["status"] = "missing_key"
                update["status_message"] = f"missing {env_key}"
                update["is_authenticated"] = False
                return preset.model_copy(update=update)
            update["is_authenticated"] = True
        if preset.provider == "codex":
            status, message, verified, default_model = _codex_readiness()
            update["status"] = status
            update["status_message"] = message
            update["is_authenticated"] = verified
            update["suggested_model"] = default_model
            return preset.model_copy(update=update)
        if preset.provider == "claude_code":
            status, message, verified, default_model = _claude_code_readiness()
            update["status"] = status
            update["status_message"] = message
            update["is_authenticated"] = verified
            update["suggested_model"] = default_model
            return preset.model_copy(update=update)
        if not preset.supports_live_catalog:
            update["status"] = "ready"
            update["status_message"] = "static catalog"
            update["is_authenticated"] = True
            return preset.model_copy(update=update)
        update["status"] = "unknown"
        update["status_message"] = ""
        update.setdefault("is_authenticated", not preset.requires_api_key)
        return preset.model_copy(update=update)

    def _lm_presets_with_status() -> list[LMProviderPreset]:
        return sorted(
            (_preset_with_status(preset) for preset in _LM_PRESETS),
            key=lambda p: p.label.lower(),
        )

    def _lm_provider_status() -> dict[str, Any]:
        status = getattr(app.state, "lm_config_status", None)
        if not isinstance(status, dict):
            return {"state": "idle"}
        return status

    def _lm_provider_info(*, presets: list[LMProviderPreset] | None = None) -> LMProviderInfo:
        cfg = dict(_effective_lm_config(app))
        # Reframed read side (design §5): the GET body reports the per-app store's
        # default profile. When the live/bound config already names a provider the
        # store default is a no-op (they are ``spec_from_config``-consistent after
        # a bind); when nothing is bound yet it surfaces the boot default profile
        # the store was seeded with, so the picker sees the effective default. This
        # fills only the identity + sampling fields the spec carries and never
        # feeds the model-ref / vision route gates (those read _effective_lm_config).
        default_spec = _default_profile_spec(app)
        has_boot_or_live_provider = bool(
            cfg.get("provider_id")
            or cfg.get("provider")
            or app.state.agent is not None
            or getattr(app.state, "want_agent", False)
        )
        if default_spec is not None and has_boot_or_live_provider:
            for key in (
                "provider_id",
                "provider",
                "api_base",
                "model",
                "provider_options",
                "temperature",
                "max_tokens",
                "thinking_budget",
            ):
                if not cfg.get(key):
                    value = getattr(default_spec, key, None)
                    if value is not None:
                        cfg[key] = value
            spec_transport = getattr(default_spec, "transport", None)
            if not cfg.get("transport") and spec_transport:
                cfg["transport"] = spec_transport
        status = _lm_provider_status()
        state = str(status.get("state") or "idle")
        if state not in {"idle", "configuring", "ready", "error"}:
            state = "idle"
        pending = status if state == "configuring" else {}
        return LMProviderInfo(
            configured=app.state.agent is not None and state != "configuring",
            provider_id=str(pending.get("provider_id") or cfg.get("provider_id", "")),
            provider=str(pending.get("provider") or cfg.get("provider", "")),
            api_base=str(pending.get("api_base") or cfg.get("api_base", "")),
            model=str(pending.get("model") or cfg.get("model", "")),
            temperature=(
                float(pending["temperature"])
                if pending.get("temperature") is not None
                else float(cfg["temperature"])
                if cfg.get("temperature") is not None
                # Mirror LMProviderConfig's deterministic default (0.0) when no
                # provider is configured yet, instead of re-surfacing the old
                # 1.0 sampler default that the agentic structured-output path
                # never wants. Keeps the idle /v1/providers/lm echo consistent
                # with what an omitted-temperature PUT actually binds.
                else 0.0
            ),
            max_tokens=(
                int(pending["max_tokens"])
                if pending.get("max_tokens") is not None
                else int(cfg["max_tokens"])
                if cfg.get("max_tokens") is not None
                else 32000
            ),
            context_length=(
                int(pending["context_length"])
                if pending.get("context_length") is not None
                else int(cfg["context_length"])
                if cfg.get("context_length") is not None
                else 0
            ),
            chosen_context=(
                int(cfg["chosen_context"]) if cfg.get("chosen_context") is not None else None
            ),
            context_window=(
                int(cfg["context_window"]) if cfg.get("context_window") is not None else None
            ),
            is_reasoning=bool(cfg.get("is_reasoning") or False),
            native_tool_calling=bool(cfg.get("native_tool_calling") or False),
            # #895 raw level, its provenance and resolved effect (never invisible).
            thinking_level=cfg.get("thinking_level"),
            thinking_level_source=cfg.get("thinking_level_source"),
            thinking_effective=str(cfg.get("thinking_effective") or ""),
            thinking_budget=(
                int(pending["thinking_budget"])
                if pending.get("thinking_budget") is not None
                else int(cfg["thinking_budget"])
                if cfg.get("thinking_budget") is not None
                else 0
            ),
            transport=pending.get("transport") or cfg.get("transport"),
            state=state,  # type: ignore[arg-type]
            status_message=str(status.get("message") or ""),
            error=str(status.get("error") or ""),
            operation_id=str(status.get("operation_id") or ""),
            provider_options=dict(cfg.get("provider_options") or {}),
            presets=presets if presets is not None else _lm_presets_with_status(),
        )

    @app.get("/v1/providers/lm", response_model=LMProviderInfo)
    async def get_lm_provider() -> LMProviderInfo:
        """Report the live LM config — what we'd report on /doctor as
        the 'lm' integration row, plus a list of presets the TUI's
        provider picker shows.

        ``configured`` is true when an agent is wired and ready to
        run; the TUI uses this to decide whether to show the config
        modal on connect.
        """

        return _lm_provider_info()

    async def _apply_lm_provider(req: LMProviderRequest) -> LMProviderInfo:
        """Reconfigure the LM in-place. Rebuilds DSPy + the
        ClioAgent so subsequent POST /messages drive the new
        provider. The old agent's state (ARC, sessions, in-flight
        messages) is preserved across the swap.
        """

        req = normalize_lm_provider_request(req, _LM_PRESETS, _default_model_for)

        def _apply_lm_studio_load_config() -> None:
            """Apply LM Studio load-time options before wiring DSPy."""

            if req.provider != "lm_studio" or req.context_length <= 0:
                return

            import requests  # noqa: PLC0415

            root = _lm_studio_api_root(req.api_base)
            if not root:
                raise RuntimeError("LM Studio api_base is empty")

            headers = _lm_studio_headers()
            # Backend concurrency cap (LM Studio "Max Concurrent Predictions").
            # Default 1: the agent fans out parallel sub-calls and a single-GPU
            # box wedges when the backend serves them concurrently, so serialize.
            _lm_studio_parallel = int(req.parallel) if req.parallel and req.parallel > 0 else 1

            def _already_loaded_with_requested_context() -> str:
                try:
                    response = requests.get(
                        f"{root}/api/v1/models",
                        headers=headers,
                        timeout=10,
                    )
                    if response.status_code >= 400:
                        return ""
                    payload = response.json()
                except Exception:  # noqa: BLE001 - unparseable instance response yields empty id
                    return ""

                models = payload.get("models")
                if not isinstance(models, list):
                    return ""
                for item in models:
                    if not isinstance(item, dict):
                        continue
                    key = str(item.get("key") or "")
                    loaded = item.get("loaded_instances")
                    if not isinstance(loaded, list):
                        continue
                    for instance in loaded:
                        if not isinstance(instance, dict):
                            continue
                        instance_id = str(instance.get("id") or "")
                        if req.model not in {key, instance_id}:
                            continue
                        config = instance.get("config")
                        if not isinstance(config, dict):
                            continue
                        try:
                            loaded_context = int(config.get("context_length") or 0)
                        except (TypeError, ValueError):
                            loaded_context = 0
                        try:
                            loaded_parallel = int(config.get("parallel") or 0)
                        except (TypeError, ValueError):
                            loaded_parallel = 0
                        # Reuse only if BOTH the context and the concurrency cap
                        # already match what we'd load — otherwise a stale
                        # parallel=4 instance would be kept and keep stalling.
                        if loaded_context == req.context_length and (
                            loaded_parallel == _lm_studio_parallel
                        ):
                            return instance_id
                return ""

            loaded_instance_id = _already_loaded_with_requested_context()
            if loaded_instance_id:
                _release_owned_lm_studio_instance(
                    app,
                    skip_instance_id=loaded_instance_id,
                    raise_on_error=True,
                )
                return

            _release_owned_lm_studio_instance(app, raise_on_error=True)
            response = requests.post(
                f"{root}/api/v1/models/load",
                headers=headers,
                json={
                    "model": req.model,
                    "context_length": req.context_length,
                    # LM Studio's "Max Concurrent Predictions". The agent issues
                    # parallel sub-calls; a single-GPU backend stalls/OOMs when it
                    # serves them concurrently, so cap it (default 1) and let
                    # concurrent pipeline calls queue. Overridable via req.parallel.
                    "parallel": _lm_studio_parallel,
                    # Flash attention drastically cuts KV-cache memory. Without it,
                    # a 9B model at a large context (e.g. 65536) on a 16GB card
                    # fills VRAM as a multi-stage agent run accumulates context and
                    # LM Studio WEDGES mid-run (the model stops responding even to a
                    # 1-token probe -> the no-progress watchdog kills the run).
                    # Enabling it is what makes the shareable local driver survive a
                    # full pipeline. Opt out with CLIO_LMSTUDIO_FLASH_ATTENTION=0.
                    "flash_attention": _lmstudio_flash_attention_enabled(),
                    "echo_load_config": True,
                },
                timeout=180,
            )
            if response.status_code >= 400:
                raise RuntimeError(
                    "LM Studio model load failed "
                    f"({response.status_code}): {(response.text or '')[:300]}"
                )
            try:
                payload = response.json()
            except Exception:  # noqa: BLE001 - unparseable response body treated as empty payload
                payload = {}
            instance_id = str(payload.get("instance_id") or "").strip()
            if instance_id:
                app.state.lm_studio_owned_instance = {
                    "root": root,
                    "instance_id": instance_id,
                    "model": req.model,
                    "context_length": req.context_length,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }

        try:
            from clio_agent.config import (
                LMProviderConfig,
                create_lm,
            )

            # Argonne / ALCF: if the TUI didn't ship an api_key, mint
            # one from the user's stored Globus session. ``LMProviderConfig``
            # will do this lazily inside __post_init__ too, but we resolve
            # eagerly here so the bound ``cfg`` (and the main agent's LMs built
            # from it) carry the real token, and so a missing token surfaces the
            # actionable structured 401 below instead of a later opaque LM error.
            resolved_api_key = req.api_key
            if req.provider == "argonne" and _is_placeholder_api_key(resolved_api_key):
                auth_exc: Exception | None
                try:
                    resolved_api_key = _resolve_argonne_runtime_api_key()
                except Exception as exc:  # noqa: BLE001 - auth failure captured in auth_exc and surfaced
                    resolved_api_key = ""
                    auth_exc = exc
                else:
                    auth_exc = None
                if not resolved_api_key:
                    raise HTTPException(
                        status_code=401,
                        detail=ErrorEnvelope(
                            error=ErrorInfo(
                                error="argonne_auth_required",
                                message=(
                                    "ALCF provider selected but no Globus token "
                                    "is available. Run "
                                    "`python -m clio_agent.providers.argonne_auth "
                                    "authenticate` once, or pass api_key in this "
                                    "request."
                                ),
                                recoverable=True,
                            )
                        ).model_dump(exclude_none=True),
                    ) from auth_exc

            is_codex, is_cc = req.provider == "codex", req.provider == "claude_code"
            cfg = LMProviderConfig(
                provider=req.provider,  # type: ignore[arg-type]  # str validated at boundary
                provider_id=req.provider_id,
                api_base=req.api_base,
                model=req.model,
                api_key=resolved_api_key or "x",
                provider_options=req.provider_options,
                temperature=req.temperature,
                max_tokens=req.max_tokens,
                top_p=req.top_p,
                top_k=req.top_k,
                min_p=req.min_p,
                presence_penalty=req.presence_penalty,
                thinking_budget=req.thinking_budget,
                thinking_level=requested_thinking_level(app, req),  # #895: see its provenance rule
                # Per-provider transport (v0.8.0): only the bound provider's field reads req.transport.
                codex_transport=(req.transport or "websocket") if is_codex else "websocket",  # type: ignore[arg-type]  # LMProviderConfig validates
                claude_code_transport=(req.transport or "sdk") if is_cc else "sdk",  # type: ignore[arg-type]  # LMProviderConfig validates; deleted values 400 typed
                codex_variant=(req.variant or "direct").lower() if is_codex else "",  # type: ignore[arg-type]
            )
            if is_cc:
                status, message, verified, default_model = _claude_code_readiness()
                if not verified:
                    from clio_agent.providers import model_discovery  # noqa: PLC0415
                    from clio_agent.providers.catalog import get_provider  # noqa: PLC0415

                    provider = get_provider(req.provider_id or req.provider)
                    if provider is not None:
                        await model_discovery.refresh_all(presets=[provider])
                    status, message, verified, default_model = _claude_code_readiness()
                if not verified:
                    raise HTTPException(
                        status_code=503 if status in {"install_required", "unavailable"} else 401,
                        detail=ErrorEnvelope(
                            error=ErrorInfo(
                                error=(
                                    "claude_code_install_required"
                                    if status == "install_required"
                                    else "claude_code_auth_required"
                                ),
                                message=message,
                                recoverable=True,
                            )
                        ).model_dump(exclude_none=True),
                    )
                if not req.model and default_model:
                    cfg.model = default_model
            if is_codex:
                await apply_codex_readiness_gate(cfg, req, _codex_readiness)
            # Per-provider handshake: discover connectivity + per-model config and
            # fold it into cfg — context-aware max_tokens (replacing the static ALCF
            # 4096 cap on 128-256K-context models), reasoning/tool capability flags,
            # and the queryable chosen_context. Never block a bind on a handshake
            # failure: fall back to the static config unchanged.
            handshake_report = None
            try:
                from clio_agent.providers.handshake import (  # noqa: PLC0415
                    HandshakeContext,
                    run_handshake,
                )

                handshake_report = await run_handshake(
                    HandshakeContext(
                        provider_id=req.provider_id or req.provider,
                        provider_kind=req.provider,
                        api_base=req.api_base,
                        api_key=resolved_api_key or "",
                        target_model=req.model,
                        auth_mode="active",
                    ),
                    force=True,
                )
                cfg.apply_handshake(handshake_report, user_set_max_tokens=(req.max_tokens or 0) > 0)
            except Exception:  # noqa: BLE001 - handshake failure recorded as a None report
                handshake_report = None
            app.state.lm_handshake_report = handshake_report
            await asyncio.get_running_loop().run_in_executor(
                None,
                _apply_lm_studio_load_config,
            )
            # Build the new LMs + adapter for the singleton main agent. The
            # process-global dspy default is deliberately NOT rewritten here
            # (design §5/§6): experts select their LM per-call via
            # ``dspy.context`` and the boot ``dspy.configure`` default remains a
            # harmless fallback for any un-wrapped ambient caller. Nothing on this
            # path mutates ``os.environ`` or dspy ``main_thread_config``, so a
            # concurrent bind can never leave a torn process-global state.
            new_lm = create_lm(cfg)
            from clio_agent.config import create_chat_adapter  # noqa: PLC0415

            new_adapter = create_chat_adapter(cfg)
            # Hot-swap the LM on the existing agent instead of
            # rebuilding from scratch. ClioAgent's expensive state
            # (ARC retriever, LSM tree, registry, expert instances,
            # tool gateways) is LM-independent — rebuilding it for
            # every Save+Connect costs ~5-10 s and is exactly the
            # latency the user complained about. These attribute
            # swaps cover the LM-dependent surface:
            #   * _provider_config   -> health/config surfaces the new provider
            #   * _main_lm           -> chat + answer synthesis use the new lm
            #   * _planner_lm        -> planner runs with the new lm
            #   * _dspy_adapter      -> local backends keep text ChatAdapter mode
            # The main agent binds these via ``dspy.context`` on every call; the
            # process-global dspy default is left untouched (design §5/§6).
            # Only rebuild from scratch when no agent yet exists
            # (first-connect lifecycle: the deferred-construction
            # task hasn't completed).
            import copy as _copy  # noqa: PLC0415

            existing = app.state.agent
            if existing is not None:
                # Publish atomically (design §5): build the fully-populated agent OFF
                # TO THE SIDE — a shallow copy that SHARES the expensive, LM-independent
                # state (ARC retriever, LSM tree, registry, expert instances, tool
                # gateways) by reference but gets its own ``__dict__`` — set its LM
                # fields, then swap ``app.state.agent`` to it in ONE pointer assignment
                # below. A concurrent reader (a turn's ``dspy.context``, a GET) therefore
                # sees either the whole old agent or the whole new agent, never a
                # half-updated singleton with ``_main_lm`` from one provider and
                # ``_dspy_adapter`` from another (the torn-read finding). The shallow
                # copy is cheap (no expert re-wiring), preserving the hot-swap latency
                # win over a from-scratch rebuild.
                agent = _copy.copy(existing)
            else:
                # Build the first agent directly with the selected, handshake-applied
                # provider.  Reading the ambient boot default here used to construct a
                # throwaway LM Studio agent first, making a clean desktop's initial
                # Codex/Claude selection wait through local-provider retries.
                agent = await construct_agent_with_relay(
                    app,
                    arc=_process_arc(app),
                    provider_config=cfg,
                )
            agent.rebind_lms(cfg)  # both paths need this cfg bound; done once, unconditionally
        except HTTPException:
            # Argonne auth path raises a structured 401 above; keep its
            # error code intact instead of flattening to a generic 400. No
            # process-global state was mutated, so there is nothing to restore.
            raise
        except Exception as exc:  # noqa: BLE001
            # Nothing mutated process-global env / dspy settings, so a failed
            # bind leaves them untouched — no restore needed.
            raise HTTPException(
                status_code=400,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="config_error",
                        message=f"failed to configure LM: {exc}",
                        details={"original_error": type(exc).__name__},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            ) from exc

        # Install/refresh the process-global dspy default from the admin bind
        # (design §6). This is the DEFAULT/admin action — the ONLY sanctioned writer
        # of the process default; experts still resolve their own LM per
        # ``dspy.context`` (no per-expert global mutation). Setting it here gives
        # every AMBIENT consumer (auto-compaction summarisation, usage/token
        # metering, the turn-end model-id probe) a valid, CURRENT LM to read when no
        # per-profile context is active. Without it: a deferred-boot GACT (started
        # without ``CLIO_LM_PROVIDER``, so the boot ``dspy.configure`` never ran) has
        # ambient ``lm=None`` and manual compaction hard-503s; and a rebind
        # (PUT A -> PUT B) leaves ambient reads pinned to the stale boot/first model.
        # Written through ``main_thread_config`` (not ``dspy.configure``) because the
        # bind runs on an executor worker thread that is not the configure-owner.
        from clio_agent.gact.runtime.ambient_lm import (  # noqa: PLC0415
            install_process_default_lm,
        )

        install_process_default_lm(new_lm, new_adapter)

        # Atomic default-profile swap (design §5). The per-app profile store is
        # immutable; ``with_default`` builds a whole new snapshot and the single
        # pointer assignment is atomic under the GIL, so a concurrent reader sees
        # either the old or the new default spec — never a torn multi-key mix.
        # This is what replaces the reverted ``lm_bind_lock`` + ``os.environ`` /
        # ``main_thread_config`` mutation: no shared mutable global, no lock.
        from clio_agent.gact.providers.profile_store import (  # noqa: PLC0415
            ProviderProfileStore,
        )
        from clio_agent.providers.lm_spec import spec_from_config  # noqa: PLC0415

        default_spec = spec_from_config(cfg)
        store = getattr(app.state, "provider_profiles", None)
        app.state.provider_profiles = (
            store.with_default(default_spec)
            if isinstance(store, ProviderProfileStore)
            else ProviderProfileStore.seed(default_spec)
        )
        # Swap the agent + ARC atomically. The old agent isn't closed (we don't
        # know what background state it owns); Python's GC cleans it up.
        mark_agent_ready(app, agent)
        # The bind swaps in a freshly-built agent (new ARCMemory); _set_app_arc
        # re-wires the arc.op op-logger (every real run binds — without it the live
        # path stays unobserved).
        _set_app_arc(app, agent.arc)
        deps.install_tool_runtime_hooks(app)
        transport = {"codex": cfg.codex_transport, "claude_code": cfg.claude_code_transport}.get(
            req.provider
        )
        app.state.lm_config = {
            "provider_id": req.provider_id or req.provider,
            "provider": req.provider,
            "api_base": req.api_base,
            "model": req.model,
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
            "context_length": req.context_length,
            "thinking_budget": req.thinking_budget,
            "thinking_level": cfg.thinking_level,  # resolved level (shipped default) #895
            **thinking_level_record(app, req),  # the person's level + its provenance
            "turn_timeout_s": req.turn_timeout_s,
            "transport": transport,
            "provider_options": dict(req.provider_options),
        }
        deps.clear_session_model_refs(app)
        # Invalidate the normalized provider catalog. It is a per-app snapshot of
        # ONE discovery pass, and delivery planning reads modalities straight out
        # of it (resource_delivery._catalog_modalities). Leaving it in place after
        # a provider swap meant the next attachment was routed against the
        # PREVIOUS provider's capability evidence -- a sticky cache deciding what
        # bytes reach a model it never described. The next GET /v1/provider-catalog
        # repopulates it; until then the planner correctly finds no evidence.
        app.state.provider_catalog = None
        # Publish so live SSE subscribers see the swap (TUI updates
        # its model chip without polling).
        app.state.bus.publish(
            Event(
                type="lm.provider.changed",
                session_id="",
                payload={
                    "provider_id": req.provider_id or req.provider,
                    "provider": req.provider,
                    "model": req.model,
                    "api_base": req.api_base,
                    "temperature": req.temperature,
                    "max_tokens": req.max_tokens,
                    "context_length": req.context_length,
                    "transport": transport,
                },
            )
        )
        return LMProviderInfo(
            configured=True,
            provider_id=req.provider_id or req.provider,
            provider=req.provider,
            api_base=req.api_base,
            model=req.model,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
            context_length=req.context_length,
            thinking_budget=req.thinking_budget,
            transport=transport,  # type: ignore[arg-type]  # values are the narrowed config Literals
            provider_options=dict(req.provider_options),
            presets=_lm_presets_with_status(),
        )

    async def _run_lm_provider_apply(req: LMProviderRequest, operation_id: str) -> None:
        try:
            loop = asyncio.get_running_loop()
            info = await loop.run_in_executor(
                None,
                lambda: asyncio.run(_apply_lm_provider(req)),
            )
        except HTTPException as exc:
            detail = exc.detail
            if isinstance(detail, dict):
                err = detail.get("error")
                if isinstance(err, dict):
                    error_code = str(err.get("error") or "config_error")
                    message = str(err.get("message") or exc)
                else:
                    error_code = "config_error"
                    message = str(detail)
            else:
                error_code = "config_error"
                message = str(detail or exc)
            app.state.lm_config_status = {
                "state": "error",
                "operation_id": operation_id,
                "provider_id": req.provider_id or req.provider,
                "provider": req.provider,
                "api_base": req.api_base,
                "model": req.model,
                "error": error_code,
                "message": message,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            app.state.bus.publish(
                Event(
                    type="lm.provider.failed",
                    session_id="",
                    payload={
                        "operation_id": operation_id,
                        "provider_id": req.provider_id or req.provider,
                        "provider": req.provider,
                        "model": req.model,
                        "error": error_code,
                        "message": message,
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001
            app.state.lm_config_status = {
                "state": "error",
                "operation_id": operation_id,
                "provider_id": req.provider_id or req.provider,
                "provider": req.provider,
                "api_base": req.api_base,
                "model": req.model,
                "error": "config_error",
                "message": f"failed to configure LM: {exc}",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            app.state.bus.publish(
                Event(
                    type="lm.provider.failed",
                    session_id="",
                    payload={
                        "operation_id": operation_id,
                        "provider_id": req.provider_id or req.provider,
                        "provider": req.provider,
                        "model": req.model,
                        "error": "config_error",
                        "message": f"failed to configure LM: {exc}",
                    },
                )
            )
        else:
            app.state.lm_config_status = {
                "state": "ready",
                "operation_id": operation_id,
                "provider_id": info.provider_id or info.provider,
                "provider": info.provider,
                "api_base": info.api_base,
                "model": info.model,
                "temperature": info.temperature,
                "max_tokens": info.max_tokens,
                "context_length": info.context_length,
                "thinking_budget": info.thinking_budget,
                "transport": info.transport,
                "message": "LM provider ready",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

    @app.put("/v1/providers/lm", response_model=LMProviderInfo)
    async def put_lm_provider(req: LMProviderRequest) -> LMProviderInfo:
        """Start or perform an LM provider swap without freezing the backend."""

        req = normalize_lm_provider_request(req, _LM_PRESETS, _default_model_for)
        running_task = getattr(app.state, "lm_config_task", None)
        if running_task is not None and not running_task.done():
            status = _lm_provider_status()
            raise HTTPException(
                status_code=409,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="provider_configuring",
                        message="LM provider configuration is already in progress.",
                        details={
                            "operation_id": status.get("operation_id", ""),
                            "provider": status.get("provider", ""),
                            "model": status.get("model", ""),
                            "recovery_actions": ["wait", "check_lm_provider_status"],
                        },
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )

        # LM Studio model loads/context changes and ALCF Globus token
        # refresh/provider wiring can block long enough to make the
        # selector feel frozen. Run those swaps in the background so
        # capability, health, agent catalog, and provider-selector
        # requests stay responsive.
        if req.provider in {"lm_studio", "argonne"}:
            operation_id = f"lmcfg_{uuid.uuid4().hex[:12]}"
            provider_label = "LM Studio" if req.provider == "lm_studio" else "ALCF"
            app.state.lm_config_status = {
                "state": "configuring",
                "operation_id": operation_id,
                "provider_id": req.provider_id or req.provider,
                "provider": req.provider,
                "api_base": req.api_base,
                "model": req.model,
                "temperature": req.temperature,
                "max_tokens": req.max_tokens,
                "context_length": req.context_length,
                "thinking_budget": req.thinking_budget,
                "transport": req.transport,
                "message": f"{provider_label} provider configuration is in progress.",
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            task = asyncio.create_task(_run_lm_provider_apply(req, operation_id))
            app.state.lm_config_task = task
            return _lm_provider_info()

        # Register the in-flight bind so the in-progress 409 guard above serialises
        # concurrent binds for EVERY provider — not only ``lm_studio``/``argonne``.
        # Cloud providers run this synchronous path; without registering the task a
        # second concurrent cloud PUT sailed past the guard and both binds mutated
        # the singleton agent's LM fields non-atomically (the torn-read finding). We
        # store the current request task (not-done while it awaits the executor
        # below), so a concurrent PUT arriving mid-bind is rejected with 409 and the
        # admitted bind wins whole-object (last-writer-wins), never field-torn. There
        # is no await between the guard check above and this assignment, so the two
        # requests cannot both observe an idle guard.
        current_task = asyncio.current_task()
        if current_task is not None:
            app.state.lm_config_task = current_task
        info = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: asyncio.run(_apply_lm_provider(req)),
        )
        app.state.lm_config_status = {
            "state": "ready",
            "operation_id": "",
            "provider_id": info.provider_id or info.provider,
            "provider": info.provider,
            "api_base": info.api_base,
            "model": info.model,
            "temperature": info.temperature,
            "max_tokens": info.max_tokens,
            "context_length": info.context_length,
            "thinking_budget": info.thinking_budget,
            "transport": info.transport,
            "message": "LM provider ready",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        return _lm_provider_info(presets=info.presets)

    @app.get("/v1/providers/lm/wait", response_model=LMProviderInfo)
    async def wait_lm_provider(timeout: float = 60.0) -> LMProviderInfo:
        """Block until the LM provider reaches a terminal state, then return it.

        The bind (``PUT /v1/providers/lm``) is async — it returns immediately and
        wires the LM through an ``idle -> configuring -> ready`` (or ``error``) state
        machine in the background. This endpoint lets any caller *await* readiness in
        a single request instead of re-implementing a client-side poll loop: it
        blocks server-side while the provider is ``configuring`` and returns the
        ``LMProviderInfo`` the moment it is ``ready``/``error`` (or ``idle`` — nothing
        pending), or when ``timeout`` (capped at 600s) elapses. Idempotent.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, min(float(timeout), 600.0))
        while True:
            status = getattr(app.state, "lm_config_status", {}) or {"state": "idle"}
            if str(status.get("state") or "idle") in ("ready", "error", "idle"):
                break
            if loop.time() >= deadline:
                break
            await asyncio.sleep(0.2)
        return _lm_provider_info()

    @app.get("/v1/providers/{provider_id}")
    async def get_provider(provider_id: str) -> dict[str, Any]:
        """SPEC §6.12 detail endpoint for one provider preset."""

        preset = next((p for p in _LM_PRESETS if p.id == provider_id), None)
        if preset is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"unknown provider: {provider_id}",
                        details={"available": [p.id for p in _LM_PRESETS]},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        return _provider_to_wire(preset)
