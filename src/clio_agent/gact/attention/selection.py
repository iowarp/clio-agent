"""Selected answer text -> producing ``lm.call`` -> generated tokens -> decode steps.

Only generated text has attention rows. A selection is a char span over one
text part of an assistant message (or a tool call's ``thought``). The producing
call is the turn's call whose output contains that text (verbatim, or
JSON-escaped inside ``submit``'s arguments); the span converts to output chars,
then to output tokens via the model's own tokenizer offsets, and output token
``t`` is decode-step row ``t`` (the step that produced it).

The connector stores no generated token ids, so that last step holds only when
re-tokenizing the recorded output gives back the captured step count: ``G`` =
tokens + 1 for a stopped request (the final row is the stop token, which has no
text), ``G`` = tokens when it hit the length limit. Checked on job 3237185:
29/29 calls. Any other count, or a capture whose health counters say rows were
skipped, is a typed reason -- never a shifted guess.
"""

from __future__ import annotations

from dataclasses import dataclass

from clio_agent.gact.attention.chat_render import ChatRenderer
from clio_agent.gact.attention.contract import AttentionRecord
from clio_agent.gact.attention.lm_calls import LmCall
from clio_agent.gact.attention.reasons import AttentionUnavailable
from clio_agent.gact.attention.textmap import Located, locate_all


@dataclass(frozen=True)
class OutputSpan:
    """Where a selection sits in the producing call's output text."""

    call: LmCall
    out_lo: int
    out_hi: int
    encoding: str
    candidates: int


def locate_output(calls: list[LmCall], part_text: str, sel_lo: int, sel_hi: int) -> OutputSpan:
    """Find the call that produced ``part_text`` and the selection's output span.

    Multiple compatible outputs are ambiguous; recency cannot establish origin.
    """
    with_payload = [c for c in calls if c.content is not None]
    if calls and not with_payload:
        raise AttentionUnavailable(
            "lm_call_payload_unavailable",
            "the turn's model calls were recorded without output text",
        )
    hits: list[tuple[LmCall, Located]] = []
    for call in with_payload:
        hits.extend((call, hit) for hit in locate_all(call.content or "", part_text))
    if not hits:
        raise AttentionUnavailable(
            "selection_not_located" if with_payload else "lm_call_not_found",
            "the answer text is not in any recorded model output of this turn",
            {"calls_searched": len(with_payload)},
        )
    if len(hits) != 1:
        raise AttentionUnavailable("lm_call_ambiguous", "multiple output passages match this part")
    call, hit = hits[0]
    out_lo, out_hi = hit.to_haystack(sel_lo, sel_hi)
    return OutputSpan(call, out_lo, out_hi, hit.encoding, len(hits))


@dataclass(frozen=True)
class StepSpan:
    """The decode steps whose rows cover a selection."""

    steps: list[int]
    token_lo: int
    token_hi: int
    output_tokens: int


def output_steps(
    renderer: ChatRenderer,
    content: str,
    out_lo: int,
    out_hi: int,
    record: AttentionRecord,
) -> StepSpan:
    """Map an output char span to decode-step rows (see the module docstring)."""
    if not record.clean:
        raise AttentionUnavailable(
            "attention_capture_partial",
            "the capture skipped or restarted decode steps, so rows no longer line up "
            "with the generated tokens",
            {"request_id": record.request_id, **record.health},
        )
    encoded = renderer.encode(content)
    n = len(encoded.ids)
    if record.decode_steps not in (n, n + 1):
        raise AttentionUnavailable(
            "output_alignment_mismatch",
            f"the recorded output re-tokenizes to {n} tokens, the capture has "
            f"{record.decode_steps} decode steps",
            {"request_id": record.request_id, "output_tokens": n},
        )
    tok_lo, tok_hi = encoded.token_span(out_lo, out_hi)
    if tok_hi <= tok_lo:
        raise AttentionUnavailable("selection_not_located", "the selection covers no tokens")
    return StepSpan(
        steps=list(range(tok_lo, tok_hi)), token_lo=tok_lo, token_hi=tok_hi, output_tokens=n
    )
