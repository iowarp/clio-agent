"""Model-capability tags: the effective facts, projected onto the shared tag vocabulary.

:func:`capability_tags` turns one :class:`~clio_agent.providers.capabilities.
combine.EffectiveCapabilities` (the per-field, precedence-resolved view of the
model / endpoint / deployment records) into a
:class:`clio_schemas.ModelCapabilityTags` -- the record the provider catalog
serves per model row and the picker renders as chips.

The projection is a set of declared FIELD-TO-TAG tables, never a match on a
model's name or description, and it adds no evidence of its own: every tag's
:class:`~clio_schemas.TagEvidence` is the source, detail and time of the
effective fact it came from. Precedence was already decided upstream
(``model_sources.resolve_model_capabilities``: user > the golden provider's own
report (OpenRouter) > overlay > server report > Hugging Face > models.dev /
LiteLLM), so this module never re-ranks sources. A fact no source states
yields no tag.

Tables (from the capability-ontology research, section 4.2):

* task (a Hub ``pipeline_tag``) -> model type (:data:`MODEL_TYPE_FOR_TASK`)
  -> role (``chat`` is general, every other type a surrogate);
* raw output modality -> the shared modality vocabulary
  (:data:`OUTPUT_MODALITY_ALIASES`: OpenRouter's ``transcription`` is text,
  ``speech`` is audio, ``rerank``/``decisions`` are scores);
* task -> the modality it produces (:data:`TASK_OUTPUT_MODALITY`), used only
  when no source states the output modalities themselves;
* tools / parallel tool calls / structured output / a thinking mechanism
  other than ``none`` -> capability tags.
"""

from __future__ import annotations

from typing import Any, get_args

from clio_schemas.model_capabilities import (
    EvidenceSource,
    ModelCapabilityTags,
    role_for_model_type,
)

from clio_agent.providers.capabilities.combine import (
    Decision,
    EffectiveCapabilities,
    ThinkingDecision,
)

#: Task (Hub ``pipeline_tag``) -> model type (LiteLLM ``mode`` spellings,
#: extended). A task not listed here is a real task with no narrower type:
#: ``other`` (a surrogate), never a guess at the nearest one.
MODEL_TYPE_FOR_TASK: dict[str, str] = {
    "text-generation": "chat",
    "image-text-to-text": "chat",
    "audio-text-to-text": "chat",
    "video-text-to-text": "chat",
    "any-to-any": "chat",
    "feature-extraction": "embedding",
    "sentence-similarity": "embedding",
    "image-feature-extraction": "embedding",
    "text-ranking": "rerank",
    "automatic-speech-recognition": "audio_transcription",
    "text-to-speech": "audio_speech",
    "text-to-audio": "audio_speech",
    "text-to-image": "image_generation",
    "unconditional-image-generation": "image_generation",
    "image-to-image": "image_edit",
    "image-text-to-image": "image_edit",
    "text-to-video": "video_generation",
    "image-to-video": "video_generation",
    "image-text-to-video": "video_generation",
    "image-to-text": "ocr",
    "mask-generation": "segmentation",
    "image-segmentation": "segmentation",
    "text-classification": "classification",
    "token-classification": "classification",
    "zero-shot-classification": "classification",
    "audio-classification": "classification",
    "image-classification": "classification",
    "zero-shot-image-classification": "classification",
    "video-classification": "classification",
    "tabular-classification": "classification",
    "time-series-forecasting": "forecasting",
}

#: The task id prefix for a task the Hub has no term for; always a surrogate
#: whose narrower type only the overlay/user can state.
_CLIO_TASK_PREFIX = "clio:"

#: A raw output modality as a source spells it -> the shared vocabulary.
#: OpenRouter's specialist output terms fold onto what the model produces;
#: anything not listed (and not already a shared term) is left out.
OUTPUT_MODALITY_ALIASES: dict[str, str] = {
    "text": "text",
    "image": "image",
    "audio": "audio",
    "video": "video",
    "pdf": "pdf",
    "embeddings": "embeddings",
    "embedding": "embeddings",
    "transcription": "text",
    "speech": "audio",
    "rerank": "scores",
    "decisions": "scores",
    "masks": "masks",
    "scores": "scores",
    "tensor": "tensor",
}

#: Task -> the modality it produces, when no source states outputs directly.
TASK_OUTPUT_MODALITY: dict[str, str] = {
    "text-generation": "text",
    "image-text-to-text": "text",
    "audio-text-to-text": "text",
    "video-text-to-text": "text",
    "feature-extraction": "embeddings",
    "sentence-similarity": "embeddings",
    "image-feature-extraction": "embeddings",
    "text-ranking": "scores",
    "text-classification": "scores",
    "zero-shot-classification": "scores",
    "image-classification": "scores",
    "audio-classification": "scores",
    "automatic-speech-recognition": "text",
    "text-to-speech": "audio",
    "text-to-audio": "audio",
    "text-to-image": "image",
    "image-to-image": "image",
    "text-to-video": "video",
    "image-to-video": "video",
    "mask-generation": "masks",
    "image-segmentation": "masks",
    "image-to-text": "text",
}

_EVIDENCE_SOURCES: frozenset[str] = frozenset(get_args(EvidenceSource))


def model_type_for_task(task: str) -> str:
    """The model type a task names.

    A ``clio:<id>`` task exists only where the Hub has no term -- a scientific
    surrogate (``clio:weather-emulation``, ``clio:pde-surrogate``); a Hub task
    with no narrower type is ``other``.
    """
    if task.startswith(_CLIO_TASK_PREFIX):
        return "scientific_surrogate"
    return MODEL_TYPE_FOR_TASK.get(task, "other")


def decision_evidence(
    decision: Decision[Any] | ThinkingDecision, *, detail: str = ""
) -> list[dict[str, str]]:
    """The :class:`~clio_schemas.TagEvidence` rows behind one effective value.

    ``decision.source`` is every agreeing fact's own source, ``+``-joined
    (:func:`~clio_agent.providers.capabilities.combine._provenance`); each
    becomes one evidence row carrying the decision's own detail and time. A
    part that names no source (``unknown``) is not evidence; a value with no
    attributable source at all yields no rows, and so no tag.
    """
    sources = [part for part in (decision.source or "").split("+") if part in _EVIDENCE_SOURCES]
    text = detail or decision.reason or ""
    return [
        {"source": source, "detail": text, "observed_at": decision.observed_at or ""}
        for source in sources
    ]


def _tag(
    value: Any, decision: Decision[Any] | ThinkingDecision, *, detail: str = ""
) -> dict[str, Any] | None:
    """One tag, or ``None`` when no source can be named for it (unknown shows nothing)."""
    evidence = decision_evidence(decision, detail=detail)
    return {"value": value, "evidence": evidence} if evidence else None


def _tags(values: list[Any], decision: Decision[Any] | ThinkingDecision) -> list[dict[str, Any]]:
    return [tag for value in values if (tag := _tag(value, decision)) is not None]


def _output_modalities(effective: EffectiveCapabilities) -> list[dict[str, Any]]:
    stated = effective.output_modalities
    if stated.known:
        mapped = sorted(
            {
                OUTPUT_MODALITY_ALIASES[raw]
                for raw in stated.value or ()
                if raw in OUTPUT_MODALITY_ALIASES
            }
        )
        return _tags(mapped, stated)
    task = effective.task
    produced = TASK_OUTPUT_MODALITY.get(task.value or "") if task.known else None
    if produced is None:
        return []
    tag = _tag(produced, task, detail=f"task {task.value} produces {produced}: {task.reason}")
    return [tag] if tag is not None else []


def _about_the_model(decision: Decision[bool]) -> bool:
    """Whether the model or its deployment (not only the endpoint software) stated it.

    An endpoint that accepts a parameter (OpenRouter's API takes
    ``response_format`` for every route) says nothing about what THIS model can
    do, so an endpoint-only ``True`` is not a capability tag.
    """
    owners = set(decision.decided_by.split("+"))
    return bool(owners & {"model", "deployment"})


def _capabilities(effective: EffectiveCapabilities) -> list[dict[str, Any]]:
    tags: list[dict[str, Any]] = []
    for name, decision in (
        ("tool_calling", effective.tools),
        ("parallel_tool_calls", effective.parallel_tool_calls),
        ("structured_output", effective.structured_output),
    ):
        if decision.value is True and _about_the_model(decision):
            tags.extend(_tags([name], decision))
    thinking = effective.thinking
    if thinking.spec is not None and thinking.spec.mechanism != "none":
        tags.extend(_tags(["reasoning"], thinking))
    return tags


def _flag(decision: Decision[bool]) -> dict[str, Any] | None:
    return _tag(decision.value, decision) if isinstance(decision.value, bool) else None


def capability_tags(effective: EffectiveCapabilities, *, model_key: str) -> ModelCapabilityTags:
    """Project one model's effective capabilities onto :class:`~clio_schemas.ModelCapabilityTags`.

    Args:
        effective: The combined view for ``(provider_id, api_base, model_id)``.
        model_key: The identity the tags describe (the linked model key, else
            the wire model id).

    Returns:
        The validated tag record. Every tag carries the evidence of the fact it
        came from; a fact no source states produces no tag.
    """
    record: dict[str, Any] = {"model_key": model_key}
    task = effective.task
    if task.known and task.value and decision_evidence(task):
        model_type = model_type_for_task(task.value)
        record["tasks"] = _tags([task.value], task)
        record["model_type"] = _tag(model_type, task)
        record["role"] = _tag(role_for_model_type(model_type), task)
    inputs = effective.input_modalities
    if inputs.known:
        record["input_modalities"] = _tags(sorted(inputs.value or ()), inputs)
    record["output_modalities"] = _output_modalities(effective)
    record["capabilities"] = _capabilities(effective)
    domains = effective.domains
    if domains.known:
        record["domains"] = _tags(sorted(domains.value or ()), domains)
    for name, decision in (("free", effective.free), ("router", effective.router)):
        flag = _flag(decision)
        if flag is not None:
            record[name] = flag
    return ModelCapabilityTags.model_validate(record)


__all__ = [
    "MODEL_TYPE_FOR_TASK",
    "OUTPUT_MODALITY_ALIASES",
    "TASK_OUTPUT_MODALITY",
    "capability_tags",
    "decision_evidence",
    "model_type_for_task",
]
