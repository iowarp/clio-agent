"""Read-only LM-provider configuration helpers for the GACT server (#714).

This module owns the *read-only* provider-bind queries carved out of
``clio_agent.gact.app``:

* :func:`_effective_lm_config` -- the configured LM (``app.state.lm_config``,
  set by ``PUT /v1/providers/lm``) merged with the live agent's effective
  ``LMProviderConfig`` so a GACT booted from ``CLIO_LM_PROVIDER`` still reports a
  complete config.
* :func:`_provider_runtime_kind` -- translate a catalog provider id (or an
  already-runtime kind) into the wire/runtime provider kind via the real
  provider registry.
* The model-ref + multimodal-capability family used by the turn-entry routes to
  validate a per-message / per-session model override against the active global
  LM and to gate image parts: :func:`_model_ref_dict`,
  :func:`_model_ref_is_empty`, :func:`_active_lm_model_ref`,
  :func:`_model_ref_matches_active`, :func:`_unsupported_model_ref_error`,
  :func:`_active_lm_supports_vision`, :func:`_image_part_error`.

All are pure reads: they only *read* ``app.state`` / normalise a value (no
mutation), and :func:`_provider_runtime_kind` only queries the registry. The
write-side bind path (``_apply_lm_provider``) lives with the provider route
handler in ``gact.app`` and is out of scope for this module. Imports stay
lazy/leaf so this module never loads ``gact.app``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

if TYPE_CHECKING:
    from fastapi import FastAPI


def _provider_runtime_kind(provider_id: str) -> str:
    """Return the wire/runtime provider kind for a catalog id or provider kind."""

    provider_id = str(provider_id or "").strip()
    if not provider_id:
        return ""
    try:
        from clio_agent.providers.catalog import get_provider  # noqa: PLC0415

        provider = get_provider(provider_id)
    except Exception:  # noqa: BLE001 - provider lookup optional; None when catalog unavailable
        provider = None
    if provider is not None:
        return str(provider.provider_kind or provider_id)
    return provider_id


def _with_native_capability_flags(app: "FastAPI", cfg: dict[str, Any]) -> dict[str, Any]:
    """Stamp the derived vision/PDF input capabilities and the arm each came from.

    Applied on EVERY return path of :func:`_effective_lm_config`, including the
    unconfigured one, so the fields the vision/PDF gates read are always
    present. ``supports_vision`` used to be read straight off the config dict
    -- a key no production writer ever set -- so the gate always fell through
    to a provider-name allowlist and the catalog's own ``supports_vision``
    flags could never reach it.

    ``supports_pdf``/``supports_pdf_source`` are stamped the SAME way, for the
    same reason: without this, :func:`_pdf_capability`'s typed reason (the
    :data:`PDF_CAPABILITY_REASONS` catalog) had zero consumers and the PDF
    capability decision never reached the effective config or the wire, so
    ``view_pdf`` could silently disappear from an agent's tool list with no
    way to see why.
    """

    supports_vision, vision_source = _vision_capability(
        app,
        str(cfg.get("provider_id") or cfg.get("provider") or ""),
        str(cfg.get("model") or ""),
    )
    cfg["supports_vision"] = supports_vision
    cfg["supports_vision_source"] = vision_source
    supports_pdf, pdf_source = _pdf_capability(
        app,
        str(cfg.get("provider_id") or cfg.get("provider") or ""),
        str(cfg.get("model") or ""),
    )
    cfg["supports_pdf"] = supports_pdf
    cfg["supports_pdf_source"] = pdf_source
    return cfg


def _effective_lm_config(app: "FastAPI") -> dict[str, Any]:
    """Return the configured LM, falling back to the live agent config.

    ``app.state.lm_config`` is populated by ``PUT /v1/providers/lm``.
    When GACT boots from ``CLIO_LM_PROVIDER`` instead, the live
    ``ClioAgent`` still carries the effective ``LMProviderConfig``.

    This stays the **gating** read: it reports the live/bound config only, and an
    unconfigured GACT (no live ``_provider_config``) reports an empty config so
    the model-ref / vision route gates behave. The per-app store's default profile
    is reported *separately* by :func:`_default_profile_spec` (used by the GET body
    builder ``_lm_provider_info``), which never feeds those gates. After a bind the
    two are ``spec_from_config``-consistent by construction.
    """

    cfg = dict(getattr(app.state, "lm_config", None) or {})
    agent = getattr(app.state, "agent", None)
    provider_config = getattr(agent, "_provider_config", None)
    if provider_config is None:
        return _with_native_capability_flags(app, cfg)

    for key in (
        "provider_id",
        "provider",
        "api_base",
        "model",
        "provider_options",
        "temperature",
        "max_tokens",
        "context_length",
        "thinking_budget",
        "chosen_context",
        "context_window",
        "is_reasoning",
        "native_tool_calling",
    ):
        if not cfg.get(key):
            value = getattr(provider_config, key, None)
            if value is not None:
                cfg[key] = value
    if not cfg.get("transport"):
        provider = getattr(provider_config, "provider", "")
        if provider == "codex":
            cfg["transport"] = getattr(provider_config, "codex_transport", None)
        elif provider == "claude_code":
            cfg["transport"] = getattr(provider_config, "claude_code_transport", None)
    if not cfg.get("codex_variant") and getattr(provider_config, "provider", "") == "codex":
        # WHICH codex transport (sdk/direct) the live agent is actually bound
        # to (S1b), for :func:`_active_lm_model_ref` to surface as
        # ``ModelRef.variant`` -- distinct from ``transport`` above.
        cfg["codex_variant"] = getattr(provider_config, "codex_variant", None)
    # Effective thinking level (#895): surface both the raw level and the resolved
    # per-provider effect so the knob is never invisible (doctor/status field-map).
    level = getattr(provider_config, "thinking_level", None)
    if level is not None:
        cfg["thinking_level"] = level
    try:
        cfg["thinking_effective"] = _thinking_effective_display(cfg)
    except Exception as exc:  # noqa: BLE001 - status must never fail on a display derivation
        # No-silent-fallback (#772): surface the degraded display with a typed
        # reason instead of omitting the field silently.
        cfg["thinking_effective"] = f"unavailable (reason=display_derivation_failed: {exc})"
    return _with_native_capability_flags(app, cfg)


def _thinking_effective_display(cfg: dict[str, Any]) -> str:
    """A human-readable ``thinking_effective`` string for the doctor/status field-map.

    Built directly off the effective capabilities (model-capabilities brief
    5.5) and the SAME :func:`~clio_agent.lm.dialect_wire.thinking_wire` the
    request builder uses -- this is a pure DISPLAY derivation of what would
    actually be sent, never a second thinking-mapping engine.
    """

    from clio_agent.lm import dialect_wire  # noqa: PLC0415
    from clio_agent.providers.capabilities import endpoint as capability_endpoint  # noqa: PLC0415
    from clio_agent.providers.capabilities.accessor import (  # noqa: PLC0415
        get_effective_capabilities,
    )
    from clio_agent.providers.catalog import get_provider  # noqa: PLC0415

    provider_id = str(cfg.get("provider_id") or cfg.get("provider") or "")
    provider_kind = str(cfg.get("provider") or "")
    model = str(cfg.get("model") or "")
    api_base = str(cfg.get("api_base") or "")
    level = cfg.get("thinking_level")
    budget = int(cfg.get("thinking_budget") or 0)

    preset = get_provider(provider_id)
    litellm_prefix = preset.litellm_prefix if preset is not None else provider_kind
    dialect = capability_endpoint.dialect_for_provider(provider_kind, litellm_prefix, provider_id)
    effective = get_effective_capabilities(provider_id, api_base, model)

    if not effective.thinking.known:
        if level not in (None, "off"):
            return (
                f"unavailable (reason=no thinking evidence for this model yet, requested={level})"
            )
        return "default (no thinking evidence for this model yet)"

    wire = dialect_wire.thinking_wire(
        dialect, effective.thinking, level=level, budget_tokens=budget
    )
    if level in (None, "off"):
        return "default (provider default)" if level is None else "off"
    if not wire:
        return f"unsupported ({effective.thinking.reason or 'not controllable here'})"
    for value in wire.values():
        if isinstance(value, dict) and isinstance(value.get("budget_tokens"), int):
            return f"{level} (budget {value['budget_tokens']})"
    return str(level)


#: The only provenance a configured global thinking level carries over with:
#: a person set it with an explicit ``thinking_level`` on ``PUT /v1/providers/lm``.
THINKING_LEVEL_SOURCE_USER = "user"


def requested_thinking_level(app: "FastAPI", req: Any) -> str | None:
    """The person's global thinking level for a ``PUT /v1/providers/lm``.

    * An explicit value is the person's choice (``null`` clears it back to the
      provider/model default).
    * An OMITTED field carries over only a level a person set before
      (``thinking_level_source == "user"``) and only when the provider and model
      are unchanged. A level is a property of one model's effort scale, and a
      shipped per-model default (``shipped_default_level``: sonnet ships ``low``)
      is not a choice -- carrying either to another model would pin it there.
    * A stored level with no source (older builds, boot config) is not a choice.

    ``None`` lets ``LMProviderConfig`` apply the new model's shipped default.
    """

    if "thinking_level" in getattr(req, "model_fields_set", set()):
        return req.thinking_level
    previous = getattr(app.state, "lm_config", None) or {}
    if previous.get("thinking_level_source") != THINKING_LEVEL_SOURCE_USER:
        return None
    same_model = (previous.get("provider_id") or previous.get("provider")) == (
        req.provider_id or req.provider
    ) and previous.get("model") == req.model
    level = previous.get("user_thinking_level")
    return str(level) if same_model and level else None


def thinking_level_record(app: "FastAPI", req: Any) -> dict[str, Any]:
    """The provenance fields the bound ``lm_config`` stores for this PUT."""

    level = requested_thinking_level(app, req)
    return {
        "user_thinking_level": level,
        "thinking_level_source": THINKING_LEVEL_SOURCE_USER if level else None,
    }


def _default_profile_spec(app: "FastAPI") -> Any:
    """Return the per-app profile store's default :class:`LMSpec`, or ``None``.

    Read-only: consults ``app.state.provider_profiles`` (the immutable, RCU-swapped
    :class:`~clio_agent.gact.providers.profile_store.ProviderProfileStore` the admin
    bind swaps and every undeclared expert inherits — design §3.4/§5). Returns
    ``None`` when no store is bound (e.g. a bare ``SimpleNamespace`` app in a unit
    test). This is the read side reading the default **off the store**; it is used
    by the GET body builder to report the bound default and never feeds the
    model-ref / vision route gates.
    """

    store = getattr(getattr(app, "state", None), "provider_profiles", None)
    if store is None:
        return None
    return getattr(store, "default", None)


def _model_ref_dict(value: Any) -> dict[str, str]:
    """Normalize a GACT ModelRef-like value to its wire keys."""

    if value is None:
        raw: Mapping[str, Any] = {}
    elif isinstance(value, Mapping):
        raw = value
    elif hasattr(value, "model_dump"):
        raw = value.model_dump(exclude_none=True)
    else:
        raw = {
            "provider_id": getattr(value, "provider_id", ""),
            "model_id": getattr(value, "model_id", ""),
            "variant": getattr(value, "variant", ""),
        }
    return {
        "provider_id": str(raw.get("provider_id") or raw.get("provider") or ""),
        "model_id": str(raw.get("model_id") or raw.get("model") or ""),
        "variant": str(raw.get("variant") or ""),
    }


def _model_ref_is_empty(value: Any) -> bool:
    """Return true when a model ref carries no selection."""

    ref = _model_ref_dict(value)
    return not any(ref.values())


def _active_lm_model_ref(app: "FastAPI") -> dict[str, str]:
    """Return the active global LM as a GACT ModelRef-shaped dict.

    ``variant`` carries the bound codex transport (``"sdk"``/``"direct"``) for
    the ``codex`` provider (S1b) so a per-message/session model ref naming a
    transport can be compared against what is actually bound; every other
    provider has no transport concept and reports ``""``.
    """

    cfg = _effective_lm_config(app)
    provider = str(cfg.get("provider_id") or cfg.get("provider") or "")
    model = str(cfg.get("model") or "")
    variant = str(cfg.get("codex_variant") or "") if cfg.get("provider") == "codex" else ""
    return {"provider_id": provider, "model_id": model, "variant": variant}


def _model_ref_matches_active(value: Any, app: "FastAPI") -> bool:
    """Return true when a requested model ref exactly matches the active LM."""

    return _model_ref_dict(value) == _active_lm_model_ref(app)


def _bare_provider_kind_error(value: Any, *, session_id: str, source: str) -> ErrorEnvelope | None:
    """A typed 400 when a model ref's ``provider_id`` is a bare provider KIND.

    A client that resolves identity by kind (the wire's ``provider`` field,
    e.g. ``"openai"``) instead of a preset's own ``provider_id`` can send that
    kind back on a message route (#1418 cause B). Where the kind also happens
    to be a real provider id (``ollama``, ``anthropic``, the direct ``openai``
    preset, ...) this is indistinguishable from a genuine selection and must
    NOT be rejected -- only a kind with no matching provider id at all
    (``argonne``, split into ``argonne_sophia`` / ``argonne_metis``) is
    unambiguous evidence of the bug. With the frontend identity fix (#1418
    cause A) this should never fire; a client that still trips it has the
    same bug the frontend had.

    Returns ``None`` when ``provider_id`` is empty, names a real preset, or
    names no known kind at all.
    """

    ref = _model_ref_dict(value)
    provider_id = ref["provider_id"]
    if not provider_id:
        return None
    from clio_agent.providers.catalog import get_provider, iter_providers  # noqa: PLC0415

    if get_provider(provider_id) is not None:
        return None
    if not any(p.provider_kind == provider_id for p in iter_providers()):
        return None
    return ErrorEnvelope(
        error=ErrorInfo(
            error="provider_kind_is_not_a_provider_id",
            message=(
                f"{source} model override names the provider KIND {provider_id!r}, "
                "not a provider id. Send the specific preset's provider_id (as "
                "reported by GET /v1/providers/lm), never its provider kind."
            ),
            details={
                "session_id": session_id,
                "source": source,
                "model": ref,
                "recovery_actions": ["put_global_lm_provider", "clear_session_model", "retry"],
            },
            recoverable=True,
        )
    )


def _unsupported_model_ref_error(
    *,
    session_id: str,
    source: str,
    model_ref: Any,
    active_model: Mapping[str, str],
) -> ErrorEnvelope:
    """Build a structured error for currently unsupported model refs."""

    return ErrorEnvelope(
        error=ErrorInfo(
            error="not_implemented",
            message=(
                f"{source} model overrides are not implemented for a model "
                "that differs from the active global LM."
            ),
            details={
                "session_id": session_id,
                "source": source,
                "model": _model_ref_dict(model_ref),
                "active_model": dict(active_model),
                "recovery_actions": [
                    "put_global_lm_provider",
                    "clear_session_model",
                    "retry",
                    "exit",
                ],
            },
            recoverable=True,
        )
    )


def _vision_capability(app: "FastAPI", provider_id: str, model_id: str) -> tuple[bool, str]:
    """Resolve image-input capability for one provider/model, with a typed reason.

    Delegates to :func:`~clio_agent.gact.modality_evidence.image_input_capability`,
    the SAME decision delivery planning and the message route use, so the gates
    cannot disagree about what a model can receive. The reason is a key of
    :data:`~clio_agent.gact.modality_evidence.IMAGE_INPUT_REASONS`: known
    modalities decide, and UNKNOWN is permitted under ``modality_unknown`` rather
    than refused. There is deliberately no provider-name allowlist and no static
    registry flag standing in for evidence.
    """

    from clio_agent.gact.modality_evidence import (  # noqa: PLC0415 - avoid import cycle
        image_input_capability,
    )
    from clio_agent.gact.types import ModelRef  # noqa: PLC0415

    return image_input_capability(app, ModelRef(provider_id=provider_id, model_id=model_id))


def _active_lm_supports_vision(app: "FastAPI") -> bool:
    """Return whether the active provider/model can receive image parts."""

    return bool(_effective_lm_config(app).get("supports_vision"))


#: Typed provenance for the active LM's PDF-document answer. Unlike image input,
#: an UNKNOWN PDF capability withholds native PDF delivery: the document still
#: reaches the model as its structured conversion, so unknown loses nothing.
PDF_CAPABILITY_REASONS: dict[str, str] = {
    "live_modality_evidence": (
        "discovery evidence for this exact provider/model states its input modalities, and "
        "they name (or omit) PDF document input"
    ),
    "modality_unknown": (
        "no evidence establishes whether this model accepts PDF input; native PDF delivery "
        "is withheld and the document is delivered as its structured conversion instead"
    ),
    "no_active_model": (
        "no provider/model is bound, so there is nothing whose capability could be evidenced"
    ),
}


def _pdf_capability(app: "FastAPI", provider_id: str, model_id: str) -> tuple[bool, str]:
    """Resolve PDF-document input capability for one provider/model, with a typed reason.

    Reads the SAME three-valued modality evidence as :func:`_vision_capability`
    and checks ``"pdf"``. Known modalities decide; unknown answers ``False``
    under ``modality_unknown`` (see :data:`PDF_CAPABILITY_REASONS`).
    """

    if not provider_id or not model_id:
        return False, "no_active_model"
    from clio_agent.gact.modality_evidence import (  # noqa: PLC0415 - avoid import cycle
        EVIDENCED_MODALITY_SOURCES,
        live_model_modalities,
    )
    from clio_agent.gact.types import ModelRef  # noqa: PLC0415

    evidence = live_model_modalities(app, ModelRef(provider_id=provider_id, model_id=model_id))
    if evidence.known and evidence.evidence in EVIDENCED_MODALITY_SOURCES:
        assert evidence.modalities is not None
        return "pdf" in evidence.modalities, "live_modality_evidence"
    return False, "modality_unknown"


def _image_part_error(
    *,
    session_id: str,
    image_count: int,
    provider: Mapping[str, Any],
) -> ErrorEnvelope:
    """Build the structured error for image parts on a text-only provider."""

    provider_id = str(provider.get("provider") or provider.get("provider_id") or "")
    model_id = str(provider.get("model") or provider.get("model_id") or "")
    return ErrorEnvelope(
        error=ErrorInfo(
            error="unsupported_multimodal_image",
            message=(
                "The selected model has no evidenced image input, so image message parts "
                "cannot be delivered. Select a model whose discovery evidence reports image "
                "input, refresh the provider's model catalog, or remove the image."
            ),
            details={
                "session_id": session_id,
                "image_part_count": image_count,
                "provider": provider_id,
                "model": model_id,
                "supports_vision": False,
                "recovery_actions": [
                    # Not a provider-name instruction: the requirement is EVIDENCE
                    # of image input for the selected model, which a refresh can
                    # produce for a provider that has never been discovered.
                    "select_a_model_with_evidenced_image_input",
                    "refresh_provider_models",
                    "remove_image_part",
                    "attach_image_as_context_file_for_tool_inspection",
                ],
            },
            recoverable=True,
        )
    )


def _current_lm_model_id() -> str:
    """Best-effort: which model the active dspy LM is bound to.

    Resolves through the ambient guard so that a read outside any per-profile
    ``dspy.context`` (e.g. turn-end metadata assembly) records a structured
    ``ambient_lm_default`` reason instead of silently depending on the process
    boot default (#818)."""
    from clio_agent.gact.runtime.ambient_lm import resolve_active_lm  # noqa: PLC0415

    lm = resolve_active_lm(site="app._current_lm_model_id")
    return getattr(lm, "model", "") if lm else ""
