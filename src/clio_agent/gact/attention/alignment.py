"""Why captured attention cannot be lined up with a recorded model call, said out loud.

Attention is mapped to text by re-tokenizing it with the canonical tokenizer: the prompt
must re-render to the captured prompt token ids, and the answer must re-tokenize to as
many tokens as the capture has decode steps (one more when the request stopped). When
either check fails no heat can be shown; instead of failing quietly, the failure is
logged as a warning and the reason the person sees names the likely cause.

The connector records no generated token ids, so the answer check compares counts. The
provider's own count of generated tokens (``usage.output_tokens`` on the ``lm.call``)
tells two common causes apart:

* the provider agrees with the capture but not with the re-tokenized text: the model
  emitted a non-canonical split of its own text (typically sampling at temperature > 0)
  or the attention tokenizer is not the model's;
* the provider agrees with the re-tokenized text but not with the capture: the capture
  does not belong to this answer, or it was cut short.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from clio_agent.gact.attention.lm_calls import LmCall
from clio_agent.gact.attention.reasons import AttentionUnavailable

logger = logging.getLogger(__name__)

#: Reasons that mean "the canonical tokenizer did not reproduce the capture".
ALIGNMENT_REASONS = frozenset({"output_alignment_mismatch", "range_alignment_mismatch"})

NON_CANONICAL = "non_canonical_tokens"
CAPTURE_MISMATCH = "capture_mismatch"
UNKNOWN = "unknown"


@contextmanager
def diagnosed(call: LmCall) -> Iterator[None]:
    """Re-raise an alignment failure inside the block with its likely cause, and log it."""

    try:
        yield
    except AttentionUnavailable as exc:
        if exc.reason not in ALIGNMENT_REASONS:
            raise
        if exc.reason == "output_alignment_mismatch":
            exc = diagnose_output(exc, call)
        logger.warning(
            "attention unavailable for session %s call %s: %s: %s",
            call.session_id,
            call.response_id or call.event_id,
            exc.reason,
            exc.detail,
        )
        raise exc from None


def diagnose_output(exc: AttentionUnavailable, call: LmCall) -> AttentionUnavailable:
    """``exc`` with the likely cause of an answer that does not re-tokenize to the capture."""

    context: dict[str, Any] = dict(exc.context)
    tokens = context.get("output_tokens")
    steps = context.get("decode_steps")
    reported = call.extra.get("output_tokens")
    finish = call.extra.get("finish_reason")
    tokenizer = context.get("tokenizer") or "the attention tokenizer"
    if isinstance(reported, int) and isinstance(steps, int) and isinstance(tokens, int):
        if abs(reported - steps) <= 1 and reported != tokens:
            cause = NON_CANONICAL
            hint = (
                f"the model generated {reported} tokens but {tokenizer} encodes the answer "
                f"text as {tokens}, so the generated tokens are not the canonical split of "
                "their own text. This usually comes from sampling (temperature > 0): use "
                "greedy decoding (temperature 0), and check that the attention tokenizer is "
                "the model's own"
            )
        elif reported == tokens:
            cause = CAPTURE_MISMATCH
            hint = (
                f"the model generated {reported} tokens and {tokenizer} agrees, but the "
                f"capture has {steps} decode steps: the attention record does not belong to "
                "this answer, or it was cut short"
            )
        else:
            cause = UNKNOWN
            hint = (
                f"the model reports {reported} generated tokens, which matches neither: "
                "check that the attention tokenizer is the model's own"
            )
    else:
        cause = UNKNOWN
        hint = (
            "the provider did not report its token count; the usual causes are sampling "
            "(temperature > 0), which can emit a non-canonical token split, or an attention "
            "tokenizer that is not the model's own"
        )
    context.update(likely_cause=cause, provider_output_tokens=reported, finish_reason=finish)
    return AttentionUnavailable(exc.reason, f"{exc.detail}. Likely cause: {hint}.", context)
