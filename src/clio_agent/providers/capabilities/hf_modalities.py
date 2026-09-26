"""Input-modality and model-type evidence from a Hugging Face repo (brief Part 6.1).

Pure functions over what :mod:`clio_agent.providers.capabilities.hf_repo`
fetched for one pinned commit -- the public metadata (``pipeline_tag``,
``config.architectures``, ``siblings``) plus whichever of ``config.json``, a
processor config or Mistral ``params.json`` was readable. Each signal is a
structured field of those files, never a match on the model's name.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from clio_agent.providers.capabilities.hf_repo import RepoResolution


#: Hub ``pipeline_tag`` -> task (``records.TASKS``: the Hub's own spelling, plus
#: two synonyms folded in). A tag not listed decides nothing.
PIPELINE_TASKS: dict[str, str] = {
    tag: tag
    for tag in (
        "text-generation",
        "image-text-to-text",
        "audio-text-to-text",
        "any-to-any",
        "text-classification",
        "feature-extraction",
        "text-ranking",
        "automatic-speech-recognition",
        "text-to-speech",
        "text-to-image",
        "text-to-video",
        "mask-generation",
        "image-segmentation",
    )
} | {
    # Hub synonyms folded onto the recorded tag.
    "sentence-similarity": "feature-extraction",
    "text-to-audio": "text-to-speech",
}

#: ``pipeline_tag`` values that name an input modality outright.
_PIPELINE_INPUT_MODALITY: dict[str, str] = {
    "image-text-to-text": "image",
    "audio-text-to-text": "audio",
}

#: Processor files a multimodal repo ships (Hub convention). Their PRESENCE is
#: what separates a text-only causal LM from a multimodal one when the config
#: itself is gated.
PROCESSOR_FILES: tuple[str, ...] = ("processor_config.json", "preprocessor_config.json")


def modalities_from_repo(
    resolution: RepoResolution,
    *,
    config: Mapping[str, Any] | None,
    processor: Mapping[str, Any] | None = None,
    params: Mapping[str, Any] | None = None,
) -> tuple[frozenset[str] | None, str]:
    """Input modalities a repo's files and metadata PROVE, with the evidence named.

    Signals, each a structured field (never a name match):

    * ``config.json``: ``vision_config`` -> image, ``audio_config`` -> audio;
    * a processor config (read when ``config.json`` is not): ``image_processor``
      -> image, ``feature_extractor`` -> audio;
    * Mistral ``params.json`` (the native format, no ``config.json``):
      ``vision_encoder`` -> image;
    * ``pipeline_tag``: ``image-text-to-text`` -> image, ``audio-text-to-text``
      -> audio.

    With none of those, the set is only KNOWN text-only when every architecture
    is a ``*ForCausalLM`` and the commit ships no processor file. A
    ``*ForConditionalGeneration`` model with a processor but no named modality
    stays UNKNOWN (``None``) -- multimodal, modality unproven.
    """
    found: dict[str, str] = {}
    if config is not None:
        if isinstance(config.get("vision_config"), Mapping):
            found.setdefault("image", "config.json vision_config")
        if isinstance(config.get("audio_config"), Mapping):
            found.setdefault("audio", "config.json audio_config")
    if processor is not None:
        if isinstance(processor.get("image_processor"), Mapping):
            found.setdefault("image", "processor config image_processor")
        if isinstance(processor.get("feature_extractor"), Mapping):
            found.setdefault("audio", "processor config feature_extractor")
    if params is not None and isinstance(params.get("vision_encoder"), Mapping):
        found.setdefault("image", "params.json vision_encoder")
    pipeline_modality = _PIPELINE_INPUT_MODALITY.get(resolution.pipeline_tag)
    if pipeline_modality:
        found.setdefault(pipeline_modality, f"pipeline_tag={resolution.pipeline_tag}")
    if found:
        detail = ", ".join(f"{modality}: {why}" for modality, why in sorted(found.items()))
        return frozenset({"text", *found}), detail

    architectures = resolution.architectures
    if not architectures and config is not None:
        raw = config.get("architectures")
        architectures = tuple(str(a) for a in raw) if isinstance(raw, list) else ()
    has_processor = any(name in resolution.siblings for name in PROCESSOR_FILES)
    if (
        architectures
        and all(arch.endswith("ForCausalLM") for arch in architectures)
        and resolution.siblings
        and not has_processor
    ):
        return frozenset({"text"}), f"text-only: architectures={list(architectures)}, no processor"
    if any(arch.endswith("ForConditionalGeneration") for arch in architectures) and has_processor:
        return None, "multimodal architecture with a processor, but no file names the modality"
    return None, "no modality evidence in the repo"


__all__ = ["PIPELINE_TASKS", "PROCESSOR_FILES", "modalities_from_repo"]
