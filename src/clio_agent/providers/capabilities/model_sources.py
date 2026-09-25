"""Model-record source precedence (brief 5.1).

A :class:`~clio_agent.providers.capabilities.records.ModelCapabilities` is
assembled from up to five layers, tried in this fixed order, PER FIELD (a
higher layer that leaves a field unknown does not block a lower layer from
filling it):

1. **user** -- an explicit override from the settings panel.
2. **overlay** -- the manual model overlay (brief Part 8). That catalog does
   not exist yet; :class:`EmptyOverlaySource` is the interface this slice
   wires in its place, so the precedence chain and its call sites never have
   to change again once P6 lands.
3. **server_report** -- what the handshake adapter itself evidenced (Ollama
   ``capabilities``, LM Studio ``trained_for_tool_use``, ...). Built by the
   adapter, passed in.
4. **hf_repo** -- the Hugging Face repo layer (brief 6.1: recommended sampling,
   a chat-template scan for thinking/tools). Fetching it is P4b;
   :class:`HfRepoSource` is the interface point.
5. **community catalogs** -- models.dev, the LiteLLM community map, and (not
   yet wired -- no fetcher exists for it in this codebase) OpenRouter's
   model-level fields. Reuses
   :mod:`clio_agent.providers.handshake.sources` (models.dev / litellm / the
   local DB) rather than fetching anything a second time.

Never left unresolved: a field nothing establishes becomes an honest
``unknown()`` :class:`~clio_agent.providers.capabilities.records.Fact`, not a
bare ``None`` sitting outside the type.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Protocol, cast

from clio_agent.providers.capabilities.records import (
    Fact,
    FactSource,
    ModelCapabilities,
    model_type_fact,
    unknown,
)

logger = logging.getLogger(__name__)

#: The field names merged, in the exact order :class:`ModelCapabilities` (and
#: therefore :func:`merge_model_layers`) declares them.
_FACT_FIELDS: tuple[str, ...] = (
    "model_type",
    "context_max",
    "output_max",
    "input_modalities",
    "tools",
    "parallel_tool_calls",
    "structured_output",
    "thinking",
    "forbidden_params",
    "sampling_thinking",
    "sampling_instruct",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class OverlaySource(Protocol):
    """A source of model-overlay facts (brief Part 8), keyed by model key."""

    def facts(self, model_key: str) -> ModelCapabilities | None:
        """Return this model's overlay-recorded facts, or ``None`` for no entry."""
        ...


class EmptyOverlaySource:
    """The P6 overlay's stand-in for this slice: always "no override".

    Brief Part 8's manual model overlay (``catalogs/models/*.yaml``) is a
    later slice. Wiring this no-op in now means :func:`resolve_model_capabilities`
    and every caller already shaped around "user > overlay > server_report >
    ..." need no changes when the real overlay lands -- only this class gets
    replaced.
    """

    def facts(self, model_key: str) -> ModelCapabilities | None:
        del model_key
        return None


class HfRepoSource(Protocol):
    """A source of Hugging Face repo facts (brief 6.1), keyed by model key.

    Fetching real Hugging Face data (``generation_config.json``, a chat-template
    scan) is P4b. No implementation is wired in this slice; a caller that has
    none simply omits ``hf_repo`` from :func:`resolve_model_capabilities`.
    """

    def facts(self, model_key: str) -> ModelCapabilities | None:
        """Return this model's Hugging Face-derived facts, or ``None``."""
        ...


def community_catalog_facts(model_id: str) -> ModelCapabilities | None:
    """Model facts from the fetched community catalogs (brief 5.1 step 5).

    Reuses :mod:`clio_agent.providers.handshake.sources` rather than fetching
    anything a second time:

    * ``context_max``/``output_max`` -- models.dev -> the LiteLLM community map
      -> the local DB (:func:`~clio_agent.providers.handshake.sources.resolve_context`
      and ``resolve_output_limit``);
    * ``input_modalities`` -- models.dev ``modalities.input``, else LiteLLM's
      ``supports_vision``/``supports_audio_input``/``supports_pdf_input``
      (:func:`~clio_agent.providers.handshake.sources.resolve_input_modalities`);
    * ``model_type`` -- LiteLLM ``mode``, else a models.dev output list that
      lacks text (:func:`~clio_agent.providers.handshake.sources.resolve_model_type`).

    Every id goes through the sources' shared normalization
    (:mod:`~clio_agent.providers.handshake.sources._normalize`), so an
    org-prefixed wire id such as ``google/gemma-4-31B-it`` matches the catalog's
    own key. OpenRouter's model-level catalog fields are the fifth named source
    (brief 5.1) but no fetcher for it exists in this codebase yet (OpenRouter's
    per-route fields read live in the handshake are DEPLOYMENT facts, not this
    tier), so this only ever returns the models.dev/litellm/db tiers.

    Returns ``None`` when the id resolves through no catalog at all; otherwise
    every field on the returned record carries its own known/unknown status --
    a catalog that states no modalities leaves ``input_modalities`` UNKNOWN,
    never "text only".
    """

    from clio_agent.providers.handshake import sources  # noqa: PLC0415

    if not (model_id or "").strip():
        return None
    observed_at = _now_iso()
    context, context_source = sources.resolve_context(model_id, "")
    output = sources.resolve_output_limit(model_id, "")
    modalities, modality_source, modality_detail = sources.resolve_input_modalities(model_id)
    model_type, type_source, type_detail = sources.resolve_model_type(model_id)
    if context is None and output is None and modalities is None and model_type is None:
        return None
    # resolve_context's provenance strings are exactly "models.dev" | "litellm" | "db"
    # (or "" on a miss) -- all valid FactSource members already.
    context_fact = (
        Fact(value=context, source=cast(FactSource, context_source), observed_at=observed_at)
        if context is not None and context_source
        else unknown()
    )
    # resolve_output_limit does not report which tier answered; models.dev is
    # tried first inside it, so that is the honest label for a hit here.
    output_fact = (
        Fact(value=output, source="models.dev", observed_at=observed_at)
        if output is not None
        else unknown()
    )
    modality_fact = (
        Fact(
            value=modalities,
            source=cast(FactSource, modality_source),
            observed_at=observed_at,
            detail=modality_detail,
        )
        if modalities is not None
        else unknown("no community catalog states this model's input modalities")
    )
    type_fact = model_type_fact(
        model_type,
        source=cast(FactSource, type_source or "unknown"),
        observed_at=observed_at,
        detail=type_detail or "no community catalog states this model's type",
    )
    return ModelCapabilities(
        model_key=model_id,
        model_type=type_fact,
        context_max=context_fact,
        output_max=output_fact,
        input_modalities=modality_fact,
    )


def merge_model_layers(model_key: str, *layers: ModelCapabilities | None) -> ModelCapabilities:
    """Merge model-capability layers field-by-field, highest precedence first.

    Args:
        model_key: The canonical key the merged record is stamped with
            (independent of which layer produced which field).
        *layers: Layers in precedence order (index 0 wins ties). ``None`` marks
            an absent layer (e.g. no overlay entry, no HF repo identified) and
            is skipped.

    Returns:
        One :class:`ModelCapabilities` whose every field is the first KNOWN
        fact found across ``layers``, or an honest ``unknown()`` fact when no
        layer establishes it at all.
    """

    resolved: dict[str, Fact] = {}
    for field_name in _FACT_FIELDS:
        chosen: Fact | None = None
        for layer in layers:
            if layer is None:
                continue
            candidate = getattr(layer, field_name)
            if isinstance(candidate, Fact) and candidate.known:
                chosen = candidate
                break
        resolved[field_name] = chosen if chosen is not None else unknown()
    return ModelCapabilities(model_key=model_key, **resolved)


def resolve_model_capabilities(
    model_key: str,
    *,
    user_override: ModelCapabilities | None = None,
    overlay: OverlaySource | None = None,
    server_report: ModelCapabilities | None = None,
    hf_repo: HfRepoSource | None = None,
    community_lookup_id: str | None = None,
) -> ModelCapabilities:
    """Resolve one model's effective facts through the full brief 5.1 precedence.

    Args:
        model_key: The canonical model key the result is stamped with.
        user_override: An explicit settings-panel override, highest precedence.
        overlay: The overlay source to consult; defaults to
            :class:`EmptyOverlaySource` (no overlay wired yet, brief Part 8/P6).
        server_report: The facts the live handshake adapter evidenced this run.
        hf_repo: The Hugging Face repo source to consult, when one is wired
            (P4b); omitted entirely when the caller has none.
        community_lookup_id: The wire/catalog id to resolve through
            :func:`community_catalog_facts`, when different from ``model_key``
            (e.g. before a link is established). Defaults to ``model_key``.

    Returns:
        The merged :class:`ModelCapabilities`, per-field precedence-resolved.
    """

    overlay_source = overlay if overlay is not None else EmptyOverlaySource()
    overlay_facts = overlay_source.facts(model_key)
    hf_facts = hf_repo.facts(model_key) if hf_repo is not None else None
    catalog_facts = community_catalog_facts(community_lookup_id or model_key)
    return merge_model_layers(
        model_key,
        user_override,
        overlay_facts,
        server_report,
        hf_facts,
        catalog_facts,
    )


__all__ = [
    "EmptyOverlaySource",
    "HfRepoSource",
    "OverlaySource",
    "community_catalog_facts",
    "merge_model_layers",
    "resolve_model_capabilities",
]
