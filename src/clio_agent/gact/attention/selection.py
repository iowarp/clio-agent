"""Selected answer text -> producing ``lm.call`` -> generated tokens -> decode steps.

Only generated text has attention rows. A selection is a char span over one
text part of an assistant message (or a tool call's ``thought``). The producing
call is the turn's call whose output contains that text (verbatim, or
JSON-escaped inside ``submit``'s arguments); the span converts to output chars,
then to output tokens via the model's own tokenizer offsets, then to decode
steps by aligning those tokens with the per-step ``token_id`` the connector
recorded -- so a re-tokenization that differs from what was generated (BPE is
not always canonical) is corrected, and a real mismatch is a typed reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

from clio_agent.gact.attention.chat_render import ChatRenderer
from clio_agent.gact.attention.lm_calls import LmCall
from clio_agent.gact.attention.reasons import AttentionUnavailable
from clio_agent.gact.attention.textmap import Located, locate

#: Below this fraction of the selected tokens matching the recorded token ids,
#: the selection is refused as ``output_alignment_mismatch`` rather than shown.
MIN_ALIGNED_FRACTION = 0.9


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

    When several calls of the turn contain the text, the latest wins (the one
    whose output became the answer) and ``candidates`` reports how many matched.
    """
    with_payload = [c for c in calls if c.content is not None]
    if calls and not with_payload:
        raise AttentionUnavailable(
            "lm_call_payload_unavailable",
            "the turn's model calls were recorded without output text",
        )
    hits: list[tuple[LmCall, Located]] = []
    for call in with_payload:
        hit = locate(call.content or "", part_text)
        if hit is not None:
            hits.append((call, hit))
    if not hits:
        raise AttentionUnavailable(
            "selection_not_located" if with_payload else "lm_call_not_found",
            "the answer text is not in any recorded model output of this turn",
            {"calls_searched": len(with_payload)},
        )
    call, hit = hits[-1]
    out_lo, out_hi = hit.to_haystack(sel_lo, sel_hi)
    return OutputSpan(call, out_lo, out_hi, hit.encoding, len(hits))


@dataclass(frozen=True)
class StepSpan:
    """The decode steps whose token rows cover a selection."""

    steps: list[int]
    token_lo: int
    token_hi: int
    aligned_fraction: float
    retokenization_exact: bool


def output_steps(
    renderer: ChatRenderer,
    content: str,
    out_lo: int,
    out_hi: int,
    step_tokens: list[tuple[int, int, int]],
) -> StepSpan:
    """Map an output char span to decode steps.

    ``step_tokens`` is ``(step, token_index, token_id)`` for every recorded
    step. The re-tokenized output is aligned to the recorded token sequence
    (ordered by ``token_index``) and each selected token maps to the step that
    carries its ``token_index``.
    """
    encoded = renderer.encode(content)
    tok_lo, tok_hi = encoded.token_span(out_lo, out_hi)
    if tok_hi <= tok_lo:
        raise AttentionUnavailable("selection_not_located", "the selection covers no tokens")
    by_index = sorted((tidx, tid, step) for step, tidx, tid in step_tokens)
    recorded_ids = [tid for _, tid, _ in by_index]
    matcher = SequenceMatcher(None, encoded.ids, recorded_ids, autojunk=False)
    ours_to_recorded: dict[int, int] = {}
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            ours_to_recorded[block.a + k] = block.b + k
    wanted = range(tok_lo, tok_hi)
    mapped = [ours_to_recorded[i] for i in wanted if i in ours_to_recorded]
    fraction = len(mapped) / len(wanted)
    if fraction < MIN_ALIGNED_FRACTION:
        raise AttentionUnavailable(
            "output_alignment_mismatch",
            f"only {len(mapped)}/{len(wanted)} selected tokens match the recorded tokens",
            {"token_lo": tok_lo, "token_hi": tok_hi},
        )
    steps = sorted({by_index[j][2] for j in mapped})
    return StepSpan(
        steps=steps,
        token_lo=tok_lo,
        token_hi=tok_hi,
        aligned_fraction=fraction,
        retokenization_exact=encoded.ids == recorded_ids,
    )
