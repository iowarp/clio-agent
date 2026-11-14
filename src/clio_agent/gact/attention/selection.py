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

A reasoning model served with a reasoning parser (vLLM ``qwen3``) generates
``<think>`` + reasoning + ``</think>`` + content, and the parser splits that into
``reasoning_content`` and ``content``. When the call recorded its reasoning and
the content alone does not match the step count, the full generated text is
rebuilt from those delimiters (only when the tokenizer has each as one token, so
the content's tokens start at a clean boundary) and must match the count
exactly; the content's steps are then offset by the prefix's tokens. Templates
that pre-open ``<think>`` in the generation prompt (Granite 4.2, Qwen3.5+) make
the model generate only ``reasoning</think>content``: the rendered prompt ending
in ``<think>`` decides which form was generated, so the stop-token tolerance can
never absorb an off-by-one prefix. A selection inside the reasoning itself maps
to the reasoning's own steps the same way.
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
    #: ``content`` (the answer) or ``reasoning`` (the call's recorded reasoning).
    source: str = "content"


def _output_text(call: LmCall, source: str) -> str | None:
    return call.reasoning if source == "reasoning" else call.content


def locate_output(
    calls: list[LmCall], part_text: str, sel_lo: int, sel_hi: int, source: str = "content"
) -> OutputSpan:
    """Find the call that produced ``part_text`` and the selection's output span.

    Multiple compatible outputs are ambiguous; recency cannot establish origin.
    """
    with_payload = [c for c in calls if _output_text(c, source) is not None]
    if calls and not with_payload:
        raise AttentionUnavailable(
            "lm_call_payload_unavailable",
            "the turn's model calls were recorded without output text",
        )
    hits: list[tuple[LmCall, Located]] = []
    for call in with_payload:
        hits.extend((call, hit) for hit in locate_all(_output_text(call, source) or "", part_text))
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
    return OutputSpan(call, out_lo, out_hi, hit.encoding, len(hits), source)


@dataclass(frozen=True)
class StepSpan:
    """The decode steps whose rows cover a selection."""

    steps: list[int]
    token_lo: int
    token_hi: int
    output_tokens: int


THINK_OPEN, THINK_CLOSE = "<think>", "</think>"


def prompt_opens_thinking(prompt_text: str) -> bool:
    """Whether the rendered generation prompt already opened ``<think>`` (Granite 4.2, Qwen3.5+)."""
    return prompt_text.rstrip().endswith(THINK_OPEN)


#: Whitespace a reasoning parser may strip between the reasoning and ``</think>``.
_THINK_SEPARATORS = ("", "\n", "\n\n")


def _reasoning_offset(
    renderer: ChatRenderer,
    reasoning: str,
    content: str,
    opened: bool = False,
    decode_steps: int | None = None,
) -> tuple[int, str] | None:
    """``(tokens, separator)`` of the generated ``[<think>]reasoning<sep></think>`` prefix, or None.

    ``<think>`` is generated unless the prompt pre-opened it. The parser may have
    stripped whitespace before ``</think>``: a separator is accepted only when the
    rebuilt output re-tokenizes to the capture's step count (``decode_steps``,
    stop row included or not). None when the delimiters are not single tokens or
    no rebuild tokenizes as prefix + content (no clean boundary).
    """
    if any(len(renderer.encode(tag).ids) != 1 for tag in (THINK_OPEN, THINK_CLOSE)):
        return None
    lead = "" if opened else THINK_OPEN
    tail = renderer.encode(content).ids
    for sep in _THINK_SEPARATORS:
        prefix = renderer.encode(lead + reasoning + sep + THINK_CLOSE).ids
        if renderer.encode(lead + reasoning + sep + THINK_CLOSE + content).ids != [*prefix, *tail]:
            continue
        if decode_steps is None or decode_steps in (
            len(prefix) + len(tail),
            len(prefix) + len(tail) + 1,
        ):
            return len(prefix), sep
    return None


def output_steps(
    renderer: ChatRenderer,
    content: str,
    out_lo: int,
    out_hi: int,
    record: AttentionRecord,
    reasoning: str | None = None,
    opened: bool = False,
    source: str = "content",
) -> StepSpan:
    """Map an output char span to decode-step rows (see the module docstring).

    ``opened``: the prompt pre-opened ``<think>`` (:func:`prompt_opens_thinking`).
    ``source="reasoning"``: ``out_lo``/``out_hi`` index ``reasoning``, not ``content``.
    """
    if not record.clean:
        raise AttentionUnavailable(
            "attention_capture_partial",
            "the capture skipped or restarted decode steps, so rows no longer line up "
            "with the generated tokens",
            {"request_id": record.request_id, **record.health},
        )
    encoded = renderer.encode(content)
    n = len(encoded.ids)
    offset = 0
    separator = ""
    if (record.decode_steps not in (n, n + 1) or source == "reasoning") and reasoning:
        found = _reasoning_offset(renderer, reasoning, content, opened, record.decode_steps)
        if found is not None:
            offset, separator = found
    if source == "reasoning":
        return _reasoning_steps(
            renderer, reasoning or "", out_lo, out_hi, record, offset, opened, separator
        )
    if record.decode_steps not in (offset + n, offset + n + 1):
        raise AttentionUnavailable(
            "output_alignment_mismatch",
            f"the recorded output re-tokenizes to {n} tokens"
            + (" (reasoning not recorded)" if not reasoning else " (with or without reasoning)")
            + f", the capture has {record.decode_steps} decode steps",
            {
                "request_id": record.request_id,
                "output_tokens": n,
                "decode_steps": record.decode_steps,
                "tokenizer": str(getattr(renderer, "identity", "") or ""),
            },
        )
    tok_lo, tok_hi = encoded.token_span(out_lo, out_hi)
    if tok_hi <= tok_lo:
        raise AttentionUnavailable("selection_not_located", "the selection covers no tokens")
    return StepSpan(
        steps=list(range(offset + tok_lo, offset + tok_hi)),
        token_lo=tok_lo,
        token_hi=tok_hi,
        output_tokens=n,
    )


def _reasoning_steps(
    renderer: ChatRenderer,
    reasoning: str,
    out_lo: int,
    out_hi: int,
    record: AttentionRecord,
    prefix: int,
    opened: bool,
    separator: str = "",
) -> StepSpan:
    """Steps of a span inside the reasoning, once the whole output aligned (``prefix`` > 0)."""
    if not prefix:
        raise AttentionUnavailable(
            "output_alignment_mismatch",
            "the reasoning could not be aligned with the capture's decode steps",
            {"request_id": record.request_id},
        )
    lead = "" if opened else THINK_OPEN
    encoded = renderer.encode(lead + reasoning)
    closed = renderer.encode(lead + reasoning + separator + THINK_CLOSE).ids
    if closed[: len(encoded.ids)] != encoded.ids or len(closed) != prefix:
        raise AttentionUnavailable(
            "output_alignment_mismatch",
            "the reasoning does not end on a token boundary before </think>",
            {"request_id": record.request_id},
        )
    tok_lo, tok_hi = encoded.token_span(out_lo + len(lead), out_hi + len(lead))
    if tok_hi <= tok_lo:
        raise AttentionUnavailable("selection_not_located", "the selection covers no tokens")
    return StepSpan(
        steps=list(range(tok_lo, tok_hi)),
        token_lo=tok_lo,
        token_hi=tok_hi,
        output_tokens=len(encoded.ids),
    )
