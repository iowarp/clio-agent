"""LM-provider catalog + runtime-bind routes for the GACT server (#714).

This concern owns the eight ``/v1/providers*`` routes the TUI's settings picker
drives:

* ``GET /v1/providers`` + ``GET /v1/providers/{provider_id}`` (SPEC §6.12) -- the
  generic provider catalog (one row per preset) and the per-provider detail row.
* ``POST /v1/providers/{provider_id}/auth`` -- kick off provider-specific auth
  (Globus OAuth for ALCF/argonne in an interactive terminal; 405 hint otherwise).
* ``GET /v1/providers/{provider_id}/models`` + ``.../handshake`` -- the per-provider
  model catalog and connectivity/auth/per-model handshake via the unified async
  handshake (passive auth -- browsing never triggers interactive OAuth).
* ``GET /v1/providers/lm`` + ``PUT /v1/providers/lm`` + ``GET /v1/providers/lm/wait``
  (CLIO-BBBBBBBBBB-D) -- report the live LM config, reconfigure it in-place (the
  async ``idle -> configuring -> ready/error`` bind), and block until the bind
  settles.

The write-side bind (``PUT /v1/providers/lm`` -> :func:`_apply_lm_provider`) is a
demoted **default-profile** action (design ``docs/design/per-expert-provider-lm.md``
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
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Request

from clio_agent.gact.events import Event
from clio_agent.gact.providers.auth import (
    _is_placeholder_api_key,
    _resolve_argonne_runtime_api_key,
)
from clio_agent.gact.providers.config import (
    _default_profile_spec,
    _effective_lm_config,
    _provider_runtime_kind,
)
from clio_agent.gact.providers.lmstudio import (
    _lm_studio_api_root,
    _lm_studio_headers,
    _release_owned_lm_studio_instance,
)
from clio_agent.gact.routes._body import json_body
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


def register_providers_routes(app: FastAPI, deps: "GactDeps") -> None:
    """Register the ``/v1/providers*`` catalog + LM-bind routes on ``app``.

    Handlers close over the ``app`` argument (FastAPI's decorators need it) and
    reach the live agent / LM config status / event bus through ``app.state``. The
    preset + model catalogs are read from :mod:`clio_agent.providers.registry` once
    at registration time (mirroring the original ``build_app`` behavior); the bind
    reaches the agent-rebuild hooks through ``deps``.
    """

    # ---- /v1/providers (#15) ------------------------------------------

    # Derived from clio_agent.providers.registry. Add new presets to
    # the registry, not here -- this list reflects whatever the registry
    # contains at registration time. Polaris preset removed for the time
    # being -- the inference-api gateway returns 400 'cluster polaris
    # does not exist' for /resource_server/polaris/vllm/v1.
    from clio_agent.providers.registry import as_lm_presets as _build_lm_presets

    _LM_PRESETS: list[LMProviderPreset] = _build_lm_presets()

    # Per-provider model catalogs. Hand-curated rather than introspected
    # because most upstreams either don't expose a /models endpoint or
    # return hundreds of irrelevant entries. The TUI's Settings → Model
    # picker calls this once per provider and lists the rows verbatim.
    # Derived from clio_agent.providers.registry. Static fallback used
    # only when live model discovery against the upstream /v1/models
    # endpoint fails (no key, network down, 5xx) -- see the GET
    # /v1/providers/{id}/models handler below for the resolution order.
    # ALCF / Argonne live model availability is dynamic (jobs spin up
    # and tear down behind the gateway); the live set can be queried
    # with `scripts/list_active_models.sh` in alcf-agentics-workflow.
    from clio_agent.providers.registry import (
        as_provider_models_dict as _build_provider_models,
    )

    _PROVIDER_MODELS: dict[str, list[dict[str, str]]] = _build_provider_models()

    def _provider_auth_state(preset: "LMProviderPreset") -> tuple[list[str], bool]:
        """Return (auth_methods, is_authenticated) for a preset.

        Maps CLIO's preset flags to the GACT v0.1 §6.12 Provider shape so
        the TUI's settings picker can render the right state badge:

        - argonne_*: globus oauth; authenticated when tokens are on disk
          AND globus-sdk is importable.
        - cloud (requires_api_key=True): api_key auth; authenticated when
          the matching env var is set.
        - local (lm_studio/ollama/codex): no auth required;
          surface as ``["none"]``, always authenticated.
        """
        if preset.provider == "argonne":
            authed = False
            try:
                from clio_agent.providers import argonne_auth  # noqa: PLC0415

                authed = (
                    argonne_auth.tokens_exist()
                    and importlib.util.find_spec("globus_sdk") is not None
                    and argonne_auth.check_auth_status()
                )
            except Exception:
                authed = False
            return ["oauth"], authed

        if preset.requires_api_key:
            env_var = {
                "anthropic": "ANTHROPIC_API_KEY",
                "openai": "OPENAI_API_KEY",
            }.get(preset.provider, "CLIO_LM_API_KEY")
            return ["api_key"], bool(os.environ.get(env_var) or os.environ.get("CLIO_LM_API_KEY"))

        return ["none"], True

    def _provider_to_wire(preset: "LMProviderPreset") -> dict[str, Any]:
        auth_methods, is_authed = _provider_auth_state(preset)
        return {
            "id": preset.id,
            "name": preset.label,
            "auth_methods": auth_methods,
            "is_authenticated": is_authed,
            "default_model": preset.suggested_model,
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

    @app.post("/v1/providers/{provider_id}/auth")
    async def auth_provider(provider_id: str, request: Request) -> dict[str, Any]:
        """SPEC §6.12 — kick off provider-specific auth.

        For argonne_*, this launches the Globus OAuth flow in an
        interactive terminal where the user can visit the URL and
        paste the generated code. This endpoint must not validate or
        refresh cached tokens inline: expired Globus sessions can
        block waiting for terminal input, which would freeze the TUI
        request instead of giving the user an actionable login path.

        Other providers (cloud / local) use api_key / no-auth and
        return 405 with a hint pointing to PUT /v1/providers/lm.
        """

        preset = next((p for p in _LM_PRESETS if p.id == provider_id), None)
        if preset is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"unknown provider: {provider_id}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )

        if preset.provider != "argonne":
            raise HTTPException(
                status_code=405,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="unsupported",
                        message=(
                            f"provider '{provider_id}' uses "
                            f"{'api_key' if preset.requires_api_key else 'no'} "
                            "auth; pass api_key directly to PUT /v1/providers/lm."
                        ),
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )

        if importlib.util.find_spec("globus_sdk") is None:
            raise HTTPException(
                status_code=503,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="dependency_missing",
                        message=(
                            "globus-sdk not installed. Install with "
                            "'pip install clio-agent[argonne]' on the "
                            "backend host and retry."
                        ),
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )

        body = await json_body(request, route="POST /v1/providers/{provider_id}/auth")
        force = bool(body.get("force", False))

        command = [
            sys.executable,
            "-m",
            "clio_agent.providers.argonne_auth",
            "authenticate",
        ]
        if force:
            command.append("--force")
        manual_command = " ".join(command)
        try:
            if os.name == "nt":
                powershell = (
                    shutil.which("pwsh.exe") or shutil.which("powershell.exe") or "powershell.exe"
                )
                command_literal = " ".join(
                    f"'{part.replace(chr(39), chr(39) + chr(39))}'" for part in command
                )
                ps_script = (
                    "$Host.UI.RawUI.WindowTitle = 'CLIO ALCF Globus Login'; "
                    "Write-Host 'CLIO ALCF Globus login'; "
                    f"Write-Host 'Running: {manual_command.replace(chr(39), chr(39) + chr(39))}'; "
                    "Write-Host ''; "
                    f"& {command_literal}; "
                    "$exitCode = $LASTEXITCODE; "
                    "Write-Host ''; "
                    "Write-Host ('Auth helper exited with code ' + $exitCode); "
                    "Read-Host 'Press Enter to close this window'"
                )
                subprocess.Popen(  # noqa: S603
                    [
                        powershell,
                        "-NoExit",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-Command",
                        ps_script,
                    ],
                    creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
                )
                instructions = (
                    "Opened a persistent PowerShell window for ALCF Globus login. Complete the "
                    "authorization code flow there, then press Ctrl+R here to refresh provider status. "
                    f"If no terminal appears, run: {manual_command}"
                )
            else:
                terminal = next(
                    (
                        shutil.which(name)
                        for name in ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm")
                        if shutil.which(name)
                    ),
                    None,
                )
                if terminal:
                    term_name = os.path.basename(terminal)
                    args = (
                        [terminal, "--", *command]
                        if term_name == "gnome-terminal"
                        else [terminal, "-e", *command]
                    )
                    subprocess.Popen(args)  # noqa: S603
                    instructions = (
                        "Opened a terminal for ALCF Globus login. Complete the "
                        "authorization code flow there, then press Ctrl+R here to refresh provider status. "
                        f"If no terminal appears, run: {manual_command}"
                    )
                else:
                    instructions = (
                        "Run this in an interactive terminal, then press Ctrl+R here: "
                        + manual_command
                    )
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="argonne_auth_failed",
                        message=f"Could not launch interactive Globus authentication: {exc}",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            ) from exc

        return {
            "is_authenticated": False,
            "provider_id": provider_id,
            "instructions": instructions,
        }

    @app.get("/v1/providers/{provider_id}/models")
    async def list_provider_models(provider_id: str, api_base: str = "") -> dict[str, Any]:
        """Per-provider model catalog via the unified async handshake.

        Resolves the preset (by id, then by bare kind) and runs the per-provider
        handshake (passive auth — browsing never triggers interactive OAuth),
        returning its ``to_models_wire`` shape with the discovered context windows
        and capability flags. Cached for the handshake TTL so spamming the picker
        doesn't hammer the upstream.

        A live provider that fails reports ``source="unavailable"`` with the reason
        rather than stale static choices; CLI providers (codex/claude_code) expose
        an editable static candidate catalog; unknown provider ids return a 404.
        """

        # Resolve the preset (by id, then by bare kind) and run the *unified*
        # handshake — provider-agnostic; the per-provider handshake class owns
        # the protocol details. Passive auth: browsing the picker must never
        # trigger an interactive OAuth flow.
        import os as _os  # noqa: PLC0415

        from clio_agent.providers.handshake import (  # noqa: PLC0415
            HandshakeContext,
            run_handshake,
        )
        from clio_agent.providers.registry import (  # noqa: PLC0415
            as_cloud_api_key_env as _cloud_env,
        )

        preset = next((p for p in _LM_PRESETS if p.id == provider_id), None)
        if preset is None:
            preset = next((p for p in _LM_PRESETS if p.provider == provider_id), None)
        if preset is None:
            # Last-ditch static for known provider ids only.
            models = _PROVIDER_MODELS.get(provider_id)
            if models is None:
                raise HTTPException(
                    status_code=404,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="not_found",
                            message=f"unknown provider: {provider_id}",
                            details={"available": sorted(_PROVIDER_MODELS)},
                            recoverable=False,
                        )
                    ).model_dump(exclude_none=True),
                )
            return {"models": models, "source": "static_catalog"}

        if preset.provider in {"codex", "claude_code"}:
            static = _PROVIDER_MODELS.get(preset.id) or _PROVIDER_MODELS.get(preset.provider)
            if static:
                return {"models": static, "source": "static_catalog"}

        env_name = _cloud_env().get(preset.provider, "")
        api_key = (
            _os.environ.get(env_name, "") if env_name else _os.environ.get("CLIO_LM_API_KEY", "")
        )
        ctx = HandshakeContext(
            provider_id=preset.id,
            provider_kind=preset.provider,
            api_base=(api_base or preset.api_base or ""),
            api_key=api_key,
            auth_mode="passive",
            allow_external_sources=True,
        )
        report = await run_handshake(ctx)
        wire = report.to_models_wire()
        # CLI providers (codex / claude_code) have no live ``/models`` endpoint, so
        # they expose an editable static candidate catalog. A *live* provider that
        # failed reports ``unavailable`` + the reason rather than showing stale
        # static choices — surfacing the problem, never silently lying with a cache.
        if not wire.get("models") and preset.provider in {"codex", "claude_code"}:
            static = _PROVIDER_MODELS.get(preset.id) or _PROVIDER_MODELS.get(preset.provider)
            if static:
                return {"models": static, "source": "static_catalog"}
        return wire

    @app.get("/v1/providers/{provider_id}/handshake")
    async def provider_handshake(
        provider_id: str, api_base: str = "", refresh: bool = False
    ) -> dict[str, Any]:
        """Async provider handshake: connectivity + auth + per-model config.

        Report-only (no runtime mutation). Runs the per-provider handshake and
        returns the discovered context windows, reasoning/tool capabilities and
        provenance alongside the legacy model list (``to_models_wire`` shape).
        Cached for the handshake TTL; ``refresh=true`` forces a re-probe. Argonne
        resolves its own stored token (passive, never interactive).
        """
        import os as _os  # noqa: PLC0415

        from clio_agent.providers.handshake import (  # noqa: PLC0415
            HandshakeContext,
            run_handshake,
        )
        from clio_agent.providers.registry import (  # noqa: PLC0415
            as_cloud_api_key_env as _cloud_env,
        )

        preset = next((p for p in _LM_PRESETS if p.id == provider_id), None)
        if preset is None:
            preset = next((p for p in _LM_PRESETS if p.provider == provider_id), None)
        if preset is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"unknown provider: {provider_id}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        env_name = _cloud_env().get(preset.provider, "")
        api_key = (
            _os.environ.get(env_name, "") if env_name else _os.environ.get("CLIO_LM_API_KEY", "")
        )
        ctx = HandshakeContext(
            provider_id=preset.id,
            provider_kind=preset.provider,
            api_base=(api_base or preset.api_base or ""),
            api_key=api_key,
            auth_mode="passive",
            allow_external_sources=True,
        )
        report = await run_handshake(ctx, force=refresh)
        out = report.to_models_wire()
        out["connectivity"] = report.connectivity.value
        out["auth"] = report.auth.value
        out["latency_ms"] = report.latency_ms
        out["generated_at"] = report.generated_at
        return out

    # ---- /v1/providers/lm (CLIO-BBBBBBBBBB-D) ------------------------

    def _normalize_lm_provider_request(req: LMProviderRequest) -> LMProviderRequest:
        """Convert catalog preset ids to runtime provider kinds before wiring DSPy."""

        preset = next((p for p in _LM_PRESETS if p.id == req.provider), None)
        if preset is None:
            return req
        provider_kind = _provider_runtime_kind(req.provider)
        if provider_kind == req.provider:
            return req
        return req.model_copy(
            update={
                "provider": provider_kind,
                "api_base": req.api_base or preset.api_base,
                "model": req.model or preset.suggested_model,
            }
        )

    def _preset_api_key_env(preset: LMProviderPreset) -> str:
        if preset.api_key_env:
            return preset.api_key_env
        return {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
        }.get(preset.id, "CLIO_LM_API_KEY")

    def _which_cli(*names: str) -> str | None:
        """Resolve a local CLI across POSIX names and Windows shims."""

        for name in names:
            found = shutil.which(name)
            if found:
                return found
            if os.name == "nt" and not name.lower().endswith((".cmd", ".exe")):
                for suffix in (".cmd", ".exe"):
                    found = shutil.which(name + suffix)
                    if found:
                        return found
        return None

    def _preset_with_status(preset: LMProviderPreset) -> LMProviderPreset:
        update: dict[str, Any] = {}
        if preset.provider == "argonne":
            env_token = (
                os.environ.get("CLIO_ARGONNE_TOKEN", "").strip()
                or os.environ.get("ALCF_INFERENCE_TOKEN", "").strip()
                or os.environ.get("access_token", "").strip()
            )
            if env_token:
                update["status"] = "ready"
                update["status_message"] = "ALCF token present in environment"
                update["is_authenticated"] = True
                return preset.model_copy(update=update)
            try:
                from clio_agent.providers import argonne_auth  # noqa: PLC0415
            except Exception as exc:
                update["status"] = "unavailable"
                update["status_message"] = f"argonne auth unavailable: {exc}"
                update["is_authenticated"] = False
                return preset.model_copy(update=update)
            if not argonne_auth.tokens_exist():
                update["status"] = "auth_required"
                update["status_message"] = (
                    "no Globus token stored; authenticate ALCF before connecting"
                )
                update["is_authenticated"] = False
                return preset.model_copy(update=update)
            if argonne_auth.check_auth_status():
                update["status"] = "ready"
                update["status_message"] = "Globus token validated"
                update["is_authenticated"] = True
                return preset.model_copy(update=update)
            update["status"] = "auth_required"
            update["status_message"] = (
                "stored Globus token could not be refreshed; authenticate ALCF"
            )
            update["is_authenticated"] = False
            return preset.model_copy(update=update)
        if preset.requires_api_key:
            env_key = _preset_api_key_env(preset)
            if not (os.environ.get(env_key) or os.environ.get("CLIO_LM_API_KEY")):
                update["status"] = "missing_key"
                update["status_message"] = f"missing {env_key}"
                update["is_authenticated"] = False
                return preset.model_copy(update=update)
            update["is_authenticated"] = True
        if preset.provider == "codex":
            if _which_cli("codex"):
                update["status"] = "ready"
                update["status_message"] = "codex CLI available"
                update["is_authenticated"] = True
            else:
                update["status"] = "unavailable"
                update["status_message"] = "codex CLI not found on PATH"
                update["is_authenticated"] = False
            return preset.model_copy(update=update)
        if preset.provider == "claude_code":
            if _which_cli("claude"):
                update["status"] = "ready"
                update["status_message"] = "claude CLI available"
                update["is_authenticated"] = True
            else:
                update["status"] = "unavailable"
                update["status_message"] = "claude CLI not found on PATH"
                update["is_authenticated"] = False
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
        if default_spec is not None:
            for key in (
                "provider",
                "api_base",
                "model",
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

        req = _normalize_lm_provider_request(req)

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
                except Exception:
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
                    "flash_attention": os.environ.get("CLIO_LMSTUDIO_FLASH_ATTENTION", "")
                    .strip()
                    .lower()
                    not in {"0", "false", "no", "off"},
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
            except Exception:
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
            from clio_agent.agent import ClioAgent
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
                except Exception as exc:
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

            cfg = LMProviderConfig(
                provider=req.provider,  # type: ignore[arg-type]  # str validated at boundary
                api_base=req.api_base,
                model=req.model,
                api_key=resolved_api_key or "x",
                temperature=req.temperature,
                max_tokens=req.max_tokens,
                top_p=req.top_p,
                top_k=req.top_k,
                min_p=req.min_p,
                presence_penalty=req.presence_penalty,
                thinking_budget=req.thinking_budget,
                codex_transport=req.transport or "exec",
                claude_code_transport=req.transport or "sdk",
            )
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
                        provider_id=req.provider,
                        provider_kind=req.provider,
                        api_base=req.api_base,
                        api_key=resolved_api_key or "",
                        target_model=req.model,
                        auth_mode="active",
                    ),
                    force=True,
                )
                cfg.apply_handshake(handshake_report, user_set_max_tokens=(req.max_tokens or 0) > 0)
            except Exception:
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
            from clio_agent.config import (  # noqa: PLC0415
                create_chat_adapter,
                create_planner_lm,
            )

            new_adapter = create_chat_adapter(cfg)
            new_planner_lm = create_planner_lm(cfg)
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
                agent._provider_config = cfg
                agent._main_lm = new_lm
                agent._planner_lm = new_planner_lm
                agent._router_lm = new_planner_lm
                agent._dspy_adapter = new_adapter
            else:
                # First-time agent construction reads the ambient boot config
                # from env; its throwaway LMs are immediately replaced by the
                # cfg-built ones below, so no env stamping is needed. Inject the
                # ONE per-process ARC so this build reuses it (no per-bind ARC churn).
                bound_arc = _process_arc(app)
                agent = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: ClioAgent(verbose=False, arc=bound_arc)
                )
                # The fresh agent built its config + LMs from env (pre-handshake);
                # carry the handshake-applied cfg + cfg-based LMs onto it so the
                # context-aware max_tokens / chosen_context are in effect on the
                # very first bind, not just on subsequent hot-swaps.
                agent._provider_config = cfg
                agent._main_lm = new_lm
                agent._planner_lm = new_planner_lm
                agent._router_lm = new_planner_lm
                agent._dspy_adapter = new_adapter
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

        # Swap the agent + ARC atomically. Old agent isn't
        # explicitly closed because we don't know what background
        # state it owns; Python's GC will clean up.
        app.state.agent = agent
        # The bind swaps in a freshly-built agent (new ARCMemory); _set_app_arc
        # re-wires the arc.op op-logger (every real run binds — without it the live
        # path stays unobserved).
        _set_app_arc(app, agent.arc)
        deps.install_tool_runtime_hooks(app)
        transport = (
            cfg.codex_transport
            if req.provider == "codex"
            else cfg.claude_code_transport
            if req.provider == "claude_code"
            else None
        )
        app.state.lm_config = {
            "provider": req.provider,
            "api_base": req.api_base,
            "model": req.model,
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
            "context_length": req.context_length,
            "thinking_budget": req.thinking_budget,
            "turn_timeout_s": req.turn_timeout_s,
            "transport": transport,
        }
        deps.clear_session_model_refs(app)
        # Publish so live SSE subscribers see the swap (TUI updates
        # its model chip without polling).
        app.state.bus.publish(
            Event(
                type="lm.provider.changed",
                session_id="",
                payload={
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
            provider=req.provider,
            api_base=req.api_base,
            model=req.model,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
            context_length=req.context_length,
            thinking_budget=req.thinking_budget,
            transport=transport,
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

        req = _normalize_lm_provider_request(req)
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
