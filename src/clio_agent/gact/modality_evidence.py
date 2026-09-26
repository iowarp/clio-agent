"""What one exact provider/model selection is evidenced to accept, three-valued.

Delivery planning (:mod:`clio_agent.gact.resource_delivery`), the message
route's image gate (:mod:`clio_agent.gact.message_submission`) and the
effective-config vision/PDF flags (:mod:`clio_agent.gact.providers.config`) all
ask the same question -- "can this model receive an image / a PDF?" -- and all
read the answer HERE, so they cannot disagree.

Two separate facts are kept apart:

* ``evidence`` -- whether (and how) the catalog holds discovery evidence that
  this exact model exists on this provider (a live probe, a persisted discovery
  run, a documented static catalog), or ``unavailable``.
* ``modalities`` -- the model's input modalities, or ``None`` when no source
  established them. A model can be live-evidenced as available while its
  modalities are still UNKNOWN (an ALCF gateway ``/models`` row carries no
  modality fields at all); that is never read as "text only".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from clio_agent.gact.types import ModelRef
from clio_agent.providers.handshake.model import resolve_model_id


@dataclass(frozen=True)
class ModalityEvidence:
    """One model's evidenced input modalities plus the evidence behind the row.

    Attributes:
        modalities: The model's input modalities, or ``None`` when UNKNOWN.
        evidence: The evidence label for the model row itself (one of
            :data:`EVIDENCED_MODALITY_SOURCES`, or ``"unavailable"``).
        generated_at: When that evidence was produced (its own timestamp, never
            the wall clock of the read).
    """

    modalities: frozenset[str] | None
    evidence: str
    generated_at: str

    @property
    def known(self) -> bool:
        """Whether the input modalities are established (as opposed to unknown)."""
        return self.modalities is not None


_UNAVAILABLE = ModalityEvidence(None, "unavailable", "")


def _modalities(values: Any) -> frozenset[str]:
    if not isinstance(values, (list, tuple, set)):
        return frozenset({"text"})
    normalized = {str(value).lower().replace("-", "_") for value in values}
    modalities = {"text"}
    if normalized & {"vision", "image", "images", "image_input"}:
        modalities.add("image")
    if normalized & {"pdf", "document", "documents", "pdf_input"}:
        modalities.add("pdf")
    if normalized & {"audio", "audio_input"}:
        modalities.add("audio")
    if normalized & {"video", "video_input"}:
        modalities.add("video")
    return frozenset(modalities)


#: Evidence-source labels that count as real discovery evidence for a model row.
#: ``live_handshake`` is a probe this process ran; ``discovery_overlay`` is a
#: persisted earlier discovery run (the Codex SDK catalog read / the claude_code
#: alias probe) served through the passive handshake; ``documented_catalog`` is
#: a static catalog row whose modalities are documented. Anything else --
#: notably ``unavailable`` -- is not evidence the model exists here at all.
EVIDENCED_MODALITY_SOURCES: frozenset[str] = frozenset(
    {"live_handshake", "discovery_overlay", "documented_catalog"}
)

#: HandshakeReport.models_source -> the delivery-plan evidence label it earns.
_EVIDENCE_LABEL_BY_SOURCE: dict[str, str] = {
    "live": "live_handshake",
    "overlay": "discovery_overlay",
}

#: capability_evidence reasons (providers/model_discovery/modality_evidence.py)
#: that count as a real, non-guessed "documented_catalog" claim on a ``static``
#: models_source row -- never a live probe, but never a guess either:
#: ``modality_documented`` is NoOpHandshake's generic registry claim and
#: ``modality_cataloged`` the Claude Code maintained-catalog fallback. The
#: negative-evidence reasons (``modality_unreported``/``modality_uncataloged``)
#: are not this arm.
DOCUMENTED_MODALITY_REASONS: frozenset[str] = frozenset(
    {"modality_documented", "modality_cataloged"}
)


def _catalog_row_aliases(row: dict[str, Any]) -> tuple[str, ...]:
    """A catalog row's own recorded aliases (:func:`~clio_agent.gact.provider_catalog.
    model_catalog_row`'s ``aliases`` field), defensively typed against a malformed payload."""

    values = row.get("aliases")
    if not isinstance(values, list):
        return ()
    return tuple(str(value) for value in values if isinstance(value, str) and value)


def _catalog_modalities(app: Any, model: ModelRef) -> ModalityEvidence:
    catalog = getattr(app.state, "provider_catalog", None)
    if not isinstance(catalog, dict):
        return _UNAVAILABLE
    providers = catalog.get("providers")
    if not isinstance(providers, list):
        return _UNAVAILABLE
    provider = next(
        (
            row
            for row in providers
            if isinstance(row, dict)
            and row.get("id") == model.provider_id
            and row.get("health") == "ready"
        ),
        None,
    )
    if provider is None or not isinstance(provider.get("models"), list):
        return _UNAVAILABLE
    rows = [row for row in provider["models"] if isinstance(row, dict)]
    # A configured model id may be an alias (e.g. claude_code's "sonnet" for
    # "claude-sonnet-5"); resolve it against this provider's own catalog rows
    # before matching, through the same resolution point ``HandshakeReport.model``
    # uses, so an alias-bound selection is never treated as an unknown model.
    canonical_id = resolve_model_id(
        ((str(row.get("model_id") or ""), _catalog_row_aliases(row)) for row in rows),
        model.model_id,
    )
    profile = next(
        (
            row
            for row in rows
            if row.get("model_id") == canonical_id
            and (
                row.get("availability") == "available"
                or (
                    isinstance(row.get("evidence"), dict)
                    and row["evidence"].get("modality_evidenced") is True
                )
            )
        ),
        None,
    )
    if profile is None or not isinstance(profile.get("evidence"), dict):
        return _UNAVAILABLE
    evidence = profile["evidence"]
    generated_at = str(evidence.get("generated_at") or "")
    # ``evidenced`` covers both a live probe and a persisted discovery run; the
    # older ``live`` key is honoured for a catalog payload written before the
    # distinction existed, so an in-flight app's cached dict is not misread.
    row_evidenced = evidence.get("evidenced")
    if row_evidenced is None:
        row_evidenced = evidence.get("live") is True
    # ``modality_evidenced`` says whether the row's ``modalities`` list is
    # established. A payload written before that key existed carried no
    # unknown state, so it falls back to the row's own evidence.
    modality_evidenced = evidence.get("modality_evidenced")
    if modality_evidenced is None:
        modality_evidenced = row_evidenced
    if row_evidenced is not True and modality_evidenced is not True:
        return ModalityEvidence(None, "unavailable", generated_at)
    source = str(evidence.get("source") or "")
    label = (
        "documented_catalog"
        if source == "static"
        else _EVIDENCE_LABEL_BY_SOURCE.get(source, "live_handshake")
    )
    modalities = _modalities(profile.get("modalities")) if modality_evidenced is True else None
    return ModalityEvidence(modalities, label, generated_at)


def live_model_modalities(app: Any, model: ModelRef) -> ModalityEvidence:
    """Return the evidenced input modalities for one exact provider/model selection."""

    report = getattr(app.state, "lm_handshake_report", None)
    if (
        report is not None
        and report.ok
        and report.models_source in _EVIDENCE_LABEL_BY_SOURCE
        # provider KIND is never an identity match (#1418): nine presets share
        # kind "openai", so matching on report.provider_kind let a message
        # routed to one provider read another same-kind provider's live
        # handshake evidence.
        and model.provider_id in {report.provider_id, ""}
    ):
        discovered = report.model(model.model_id)
        if discovered is not None:
            from clio_agent.providers.capabilities.accessor import (  # noqa: PLC0415
                get_effective_capabilities,
            )

            effective = get_effective_capabilities(
                report.provider_id, report.api_base, discovered.id
            )
            # The evidence's OWN timestamp, not the wall clock of the handshake
            # run that read it -- a cached catalog must not date itself to now.
            return ModalityEvidence(
                (
                    frozenset(effective.input_modalities.value or ())
                    if effective.input_modalities.known
                    else None
                ),
                _EVIDENCE_LABEL_BY_SOURCE[report.models_source],
                discovered.evidence_generated_at
                or getattr(report, "evidence_generated_at", "")
                or report.generated_at,
            )
    if report is not None and report.ok and report.models_source == "static":
        discovered = report.model(model.model_id)
        evidence = discovered.raw.get("capability_evidence") if discovered is not None else None
        if isinstance(evidence, dict) and evidence.get("reason") in DOCUMENTED_MODALITY_REASONS:
            from clio_agent.providers.capabilities.accessor import (  # noqa: PLC0415
                get_effective_capabilities,
            )

            assert discovered is not None
            effective = get_effective_capabilities(
                report.provider_id, report.api_base, discovered.id
            )
            return ModalityEvidence(
                (
                    frozenset(effective.input_modalities.value or ())
                    if effective.input_modalities.known
                    else None
                ),
                "documented_catalog",
                report.evidence_generated_at or report.generated_at,
            )
    return _catalog_modalities(app, model)


#: Typed provenance for an image-input answer. Each arm says WHY the gate answered
#: as it did, so a refusal is explainable and a permission is auditable.
IMAGE_INPUT_REASONS: dict[str, str] = {
    "live_modality_evidence": (
        "discovery evidence for this exact provider/model states its input modalities, and "
        "they name (or omit) image input"
    ),
    "modality_unknown": (
        "no evidence establishes whether this model accepts image input (its discovery "
        "row states no modalities, or it has not been discovered yet); unknown is not "
        "refused -- the image is sent and the upstream endpoint decides"
    ),
    "no_active_model": (
        "no provider/model is bound, so there is nothing whose capability could be evidenced"
    ),
}


def image_input_capability(app: Any, model: ModelRef) -> tuple[bool, str]:
    """Whether ``model`` may receive image parts, with an :data:`IMAGE_INPUT_REASONS` key.

    Only KNOWN modalities that omit image refuse. Unknown -- the model's row states
    no modalities, or no row exists yet -- is permitted with the typed
    ``modality_unknown`` reason rather than refused: absence of evidence is not
    evidence of a text-only model.
    """

    if not model.provider_id or not model.model_id:
        return False, "no_active_model"
    evidence = live_model_modalities(app, model)
    if evidence.known and evidence.evidence in EVIDENCED_MODALITY_SOURCES:
        assert evidence.modalities is not None
        return "image" in evidence.modalities, "live_modality_evidence"
    return True, "modality_unknown"


__all__ = [
    "DOCUMENTED_MODALITY_REASONS",
    "EVIDENCED_MODALITY_SOURCES",
    "IMAGE_INPUT_REASONS",
    "ModalityEvidence",
    "image_input_capability",
    "live_model_modalities",
]
