"""Split Claude Code SDK ``thinking_delta`` text into hidden provider thinking and
the DSPy ChatAdapter contract.

The Claude Code SDK can stream the DSPy contract on the ``thinking_delta`` channel
before it later emits a bursty ``text_delta`` copy. This module owns the boundary
detection that decides where the provider's free-form extended thinking ends and the
structured contract begins, so :mod:`clio_agent.providers.claude_code_litellm` stays a
thin transport and the (subtle) marker logic lives in one testable place (#877/#880).
"""

from __future__ import annotations

import re

# A DSPy ChatAdapter field header at the START of a line. This mirrors DSPy's own output
# grammar and parser: ``chat_adapter.py`` uses ``field_header_pattern = r"\[\[ ## (\w+) ## \]\]"``
# matched per line via ``field_header_pattern.match(line.strip())`` — a header is only a header
# at a line start. Both anchors are load-bearing:
#   * Line-start (``\A`` or after ``\n``): a mid-line MENTION of a marker — the model narrating
#     its own contract in prose, e.g. ``... it emits `[[ ## next_thought ## ]]` then ...`` — is
#     NOT a contract boundary and stays in provider thinking, so it never latches the contract
#     and never leaks to the render as garbled scaffolding (#877).
#   * ``\w+`` (not a fixed allowlist): a well-formed header for an UNKNOWN field name is still
#     recognized as contract, matching DSPy's grammar, rather than surviving in provider
#     thinking and rendering verbatim once the client marker strip is deleted (#880).
_CONTRACT_HEADER_RE = re.compile(r"(?:\A|\n)[ \t]*\[\[ ## \w+ ## \]\]")

# The literal prefix that opens a header, up to (but not including) the ``\w+`` field name.
# Used to size the straddle hold-back when a header is split across two ``thinking_delta``s.
_HEADER_LITERAL = "[[ ## "


def _partial_header_prefix_len(combined: str) -> int:
    """Length of the trailing suffix of ``combined`` that could still complete into a
    line-start ``[[ ## <field> ## ]]`` header once more text arrives (a straddled marker).

    Only the last (incomplete) line can host a forming header: earlier lines are terminated
    by ``\\n`` and were already scanned for a completed header. Whitespace-only or clearly
    non-header trailing lines return 0 — they are safe to flush now, because a header on the
    next delta still matches at ``\\A``/``\\n``.
    """

    segment = combined[combined.rfind("\n") + 1 :]
    stripped = segment.lstrip(" \t")
    if stripped == "":
        return 0
    if len(stripped) <= len(_HEADER_LITERAL):
        return len(segment) if _HEADER_LITERAL.startswith(stripped) else 0
    if not stripped.startswith(_HEADER_LITERAL):
        return 0
    after = stripped[len(_HEADER_LITERAL) :]
    name = re.match(r"\w+", after)
    if name is None:
        return 0
    return len(segment) if " ## ]]".startswith(after[name.end() :]) else 0


def _split_provider_thinking_contract_delta(
    text: str,
    *,
    marker_tail: str,
    contract_started: bool,
) -> tuple[str, str, str, bool]:
    """Split one SDK ``thinking_delta`` into hidden provider thinking and DSPy contract text.

    Once a ``[[ ## field ## ]]`` header appears **at the start of a line** (DSPy's own header
    grammar — see :data:`_CONTRACT_HEADER_RE`), that suffix is no longer merely provider-internal
    thinking: it is the model's structured contract and must enter the normal LiteLLM text stream
    immediately so field extractors can publish visible deltas over time. A mid-line marker
    mention (the model narrating the format in prose) is left in provider thinking and never
    latches the contract (#877).

    Returns ``(provider_thinking, contract_text, next_tail, next_started)``.
    """

    if not text:
        return "", "", marker_tail, contract_started
    if contract_started:
        return "", text, marker_tail, True

    combined = marker_tail + text
    header = _CONTRACT_HEADER_RE.search(combined)
    if header is not None:
        start = header.start()
        if combined[start : start + 1] == "\n":
            start += 1  # keep the boundary newline with provider thinking
        return combined[:start], combined[start:], "", True

    hold = _partial_header_prefix_len(combined)
    if hold == 0:
        return combined, "", "", False
    split = len(combined) - hold
    return combined[:split], "", combined[split:], False
