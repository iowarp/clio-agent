"""Typed availability reasons for the attention view (no silent fallback)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

AttentionReason = Literal[
    # configuration / provider
    "attention_disabled",
    "provider_not_vllm",
    "flowcept_not_configured",
    "attention_tokenizer_unavailable",
    # transcript / CLIO provenance
    "message_not_found",
    "message_not_generated",
    "lm_call_store_unavailable",
    "lm_call_not_found",
    "lm_call_ambiguous",
    "lm_call_payload_unavailable",
    "ranges_not_declared",
    "response_id_missing",
    "selection_not_located",
    # connector store (see contract.py)
    "attention_query_failed",
    "attention_record_not_found",
    "attention_arrays_not_in_store",
    "attention_capture_failed",
    "attention_steps_missing",
    "attention_record_malformed",
    # alignment checks
    "range_alignment_mismatch",
    "output_alignment_mismatch",
]

#: Human-readable text per reason, shown verbatim by the UI next to the
#: disabled action. Kept here so every surface explains a reason the same way.
REASON_TEXT: dict[str, str] = {
    "attention_disabled": "Attention capture is off (provenance.attention).",
    "provider_not_vllm": "This answer came from a provider without attention capture.",
    "flowcept_not_configured": "Flowcept provenance is not configured.",
    "attention_tokenizer_unavailable": "The model's tokenizer could not be loaded.",
    "message_not_found": "The message is not in this session.",
    "message_not_generated": "Only generated answers have attention rows.",
    "lm_call_store_unavailable": "No provenance store holds this session's model calls.",
    "lm_call_not_found": "The model call that produced this answer was not recorded.",
    "lm_call_ambiguous": "More than one model call matches this answer.",
    "lm_call_payload_unavailable": "The model call was recorded without its content.",
    "ranges_not_declared": "Section ranges were not declared for this model call.",
    "response_id_missing": "The model call has no provider response id.",
    "selection_not_located": "The selected text was not found in the model output.",
    "attention_query_failed": "The attention store query failed.",
    "attention_record_not_found": "No attention record exists for this model call yet.",
    "attention_arrays_not_in_store": "The attention record holds only a file reference.",
    "attention_capture_failed": "The attention capture failed for this model call.",
    "attention_steps_missing": "Attention rows for the selected tokens are missing.",
    "attention_record_malformed": "The attention record does not match the contract.",
    "range_alignment_mismatch": "Declared sections do not match the captured segments.",
    "output_alignment_mismatch": "The answer's tokens do not match the captured steps.",
}


@dataclass
class AttentionUnavailable(Exception):
    """Raised (and served) when attention cannot be shown, with a typed reason."""

    reason: AttentionReason
    detail: str = ""
    context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(f"{self.reason}: {self.detail}")

    def to_wire(self) -> dict[str, Any]:
        """Serialize for the HTTP surface."""
        return {
            "available": False,
            "reason": self.reason,
            "message": REASON_TEXT.get(self.reason, self.reason),
            "detail": self.detail,
            "context": self.context,
        }
