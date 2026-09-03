"""Typed provenance for a discovered model's input modalities.

Modality capability must come from EVIDENCE, never fabrication. Two concrete
fabrication routes were found and closed here:

* the official ``openai_codex`` SDK declares ``Model.input_modalities`` with a
  schema default of ``["text", "image"]``, so a wire row that OMITS the field
  hands back an image capability nobody reported;
* the ``claude_code`` alias probe stamped a hardcoded ``["text", "image",
  "pdf"]`` on every non-error reply, so a CLI that silently stripped the
  attachments still "proved" both modalities.

Both now record only what a provider actually said, and name the gap with a
typed reason in the ``stream_fallback`` reason-catalog style: the code is the
queryable fact, the sentence is what a human reads. The evidence rides on the
discovered row (``capability_evidence``), so it is persisted onto the refresh
overlay, read back by the passive handshake, and reaches the provider-catalog
wire rather than being lost at the discovery boundary.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

#: Where a modality claim came from. A source is not evidence on its own — it
#: names WHICH surface was consulted, and the reason says what it yielded.
MODALITY_SOURCES: dict[str, str] = {
    "codex_sdk_input_modalities": ("the Codex Python SDK's model row (``Model.input_modalities``)"),
    "claude_code_native_probe": (
        "one claude_code CLI turn carrying a known image and PDF whose content the "
        "reply must quote back"
    ),
}

#: Typed modality-evidence reasons. Every discovered row carries exactly one.
MODALITY_EVIDENCE_REASONS: dict[str, str] = {
    "modality_reported": (
        "the provider reported this model's input modalities explicitly; the recorded "
        "capabilities are exactly what it said"
    ),
    "modality_unreported": (
        "the provider omitted input modalities for this model. The SDK's schema default "
        "was NOT adopted as evidence, so no non-text modality is recorded and the typed "
        "negative can fire; re-run discovery once the provider reports the field"
    ),
    "modality_probe_unevidenced": (
        "a live multimodal probe ran but the reply did not quote back the attached "
        "content, so the attachment may never have reached the model; the unevidenced "
        "modalities are not recorded as capabilities"
    ),
    "modality_probe_unavailable": (
        "the multimodal probe could not run (or failed) and a text-only probe validated "
        "the model instead, so every non-text modality is unreported rather than assumed"
    ),
}


class UnknownModalityReasonError(ValueError):
    """Raised when a caller invents a modality-evidence reason outside the catalog."""


def modality_evidence(
    *,
    source: str,
    reason: str,
    unevidenced: Sequence[str] = (),
    detail: str = "",
) -> dict[str, Any]:
    """Build one typed ``capability_evidence`` record for a discovered model row.

    Args:
        source: A key of :data:`MODALITY_SOURCES` naming the consulted surface.
        reason: A key of :data:`MODALITY_EVIDENCE_REASONS`.
        unevidenced: Modalities this row could NOT evidence (recorded so a
            consumer can tell "the provider says no" from "nobody asked").
        detail: Optional free text (a probe reply excerpt, an error string).

    Returns:
        The evidence record persisted onto the overlay row and surfaced on the
        provider-catalog wire.

    Raises:
        UnknownModalityReasonError: for a source or reason outside the catalogs —
            the same reject-unknowns discipline the stream_fallback catalog uses.
    """

    if source not in MODALITY_SOURCES:
        raise UnknownModalityReasonError(f"Unknown modality evidence source: {source}")
    if reason not in MODALITY_EVIDENCE_REASONS:
        raise UnknownModalityReasonError(f"Unknown modality evidence reason: {reason}")
    record: dict[str, Any] = {
        "source": source,
        "source_description": MODALITY_SOURCES[source],
        "reason": reason,
        "description": MODALITY_EVIDENCE_REASONS[reason],
    }
    if unevidenced:
        record["unevidenced"] = sorted(
            {str(value).strip().lower() for value in unevidenced if value}
        )
    if detail:
        record["detail"] = detail
    return record


def reported_modalities(row: Any, field_name: str) -> list[str] | None:
    """Return a provider row's EXPLICITLY reported modalities, or ``None``.

    ``None`` means "the row did not report the field" — distinct from an empty
    list, which is a provider explicitly reporting no modalities. A Pydantic row
    (every real SDK response) is judged by ``model_fields_set``, which holds only
    the fields the wire actually carried; a schema default therefore never
    passes as evidence. A row that exposes no ``model_fields_set`` at all cannot
    tell us what it was told, so it is treated as unreported rather than trusted.
    """

    fields_set = getattr(row, "model_fields_set", None)
    if not isinstance(fields_set, (set, frozenset)) or field_name not in fields_set:
        return None
    values = getattr(row, field_name, None)
    if values is None:
        return None
    return [str(getattr(value, "value", value)) for value in values]


__all__ = [
    "MODALITY_EVIDENCE_REASONS",
    "MODALITY_SOURCES",
    "UnknownModalityReasonError",
    "modality_evidence",
    "reported_modalities",
]
