"""Reading + parsing the agent-elicitation answer child's reply (#1309, C1-S7).

Split out of :mod:`clio_agent.gact.agent_elicitation` (the cleanup program's
no-accretion rule, #775: that module is a ratchet-baselined file — new logic
goes in an owner module of its own, not appended past its recorded line
count) as its own small, focused owner for exactly one concern: given the
answer child turn's own final message, produce the raw reply text the
server-declared JSON contract lives in, then parse it structurally.

ROOT CAUSE this module fixes (the live #1309 C1-S7 avenue,
``out/live-verification/leg_c2_verdict.json``: ``agent_elicitation_routing``
== ``elicitation_routed_to_agent`` -> fallback, detail
``agent_answer_unparseable``): the answer turn's ``chain_of_thought`` module
(the F1 tool-allowlist mint's kind flip in ``agent_elicitation.py``) emits its
``reasoning`` field as a genuinely VISIBLE ``text`` part — that expert kind's
ENTIRE visible conversation, never suppressed (#878;
``tests/test_gact/test_react_extract_suppression.py``'s pinned truth table:
``chain_of_thought``/``predict``/off-scope are never gated) — landing in the
SAME final message as the declared JSON ``answer`` field's own separate part.
The generic ``turn_spawn.py::_message_text``/``answer_excerpt`` reader joins
EVERY ``text``-type part of a message regardless of which DSPy contract field
produced it, so the reasoning prose concatenates directly onto the JSON with
no separator — exactly what a live, genuinely-streamed CoT turn produced.
This is a READER defect, not a render one: the render/transcript pipeline
classifying CoT ``reasoning`` as visible ``text`` is itself correct and
pinned (never touched here) — :func:`answer_field_text` instead reads ONLY
the ``answer``-tagged part(s), and :func:`parse_agent_reply` hardens the
strict JSON contract with a structural (never prose-scraping) balanced-block
extraction as defense in depth.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def answer_field_text(app: Any, session_id: str, message_ref: str) -> str:
    """The answer child's own ``answer``-field text, read directly off its
    final message — untruncated and never mixed with its ``reasoning`` prose.

    Reads directly off ``app.state.messages`` (RULE 4: no new store) by the
    exact ``message_ref`` the generic spawn-completion hook already resolved
    (:mod:`clio_agent.gact.turn_spawn`'s ``result["message_ref"]``) rather
    than through the generic ``answer_excerpt`` (that module's 2000-char,
    all-text-fields cap built for OTHER spawn/wait consumers), so a long
    ``reasoning`` block can never truncate a legitimately long JSON answer
    before it is even read (the #1309 fix's truncation-interaction
    disposition).
    """

    if not message_ref:
        return ""
    messages = getattr(app.state, "messages", {})
    rows = messages.get(session_id, []) if hasattr(messages, "get") else []
    msg = next((m for m in rows if str(getattr(m, "id", "") or "") == message_ref), None)
    if msg is None:
        return ""
    parts = getattr(msg, "parts", None) or []
    out: list[str] = []
    for part in parts:
        part_type = getattr(part, "type", None) or (
            part.get("type") if isinstance(part, Mapping) else None
        )
        if part_type != "text":
            continue
        metadata = getattr(part, "metadata", None) or (
            part.get("metadata") if isinstance(part, Mapping) else None
        )
        field = metadata.get("signature_field_name") if isinstance(metadata, Mapping) else None
        if field != "answer":
            continue
        text = getattr(part, "text", None)
        if text is None and isinstance(part, Mapping):
            text = part.get("text")
        if text:
            out.append(str(text))
    return "".join(out).strip()


def _contract_object_from_candidate(candidate: str) -> dict[str, Any] | None:
    """Strict ``json.loads`` + the declared answer/decline Mapping contract on
    ONE candidate string. No prose interpretation: a candidate that fails to
    parse, or parses to anything but a ``Mapping`` carrying ``answer`` or
    ``decline``, is :data:`None`."""

    try:
        parsed = json.loads(candidate)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, Mapping):
        return None
    if "answer" not in parsed and "decline" not in parsed:
        return None
    return dict(parsed)


def _first_balanced_json_object(text: str) -> str | None:
    """The first STRUCTURALLY balanced top-level ``{...}`` block in ``text``.

    Bracket-depth counting only (never a keyword/regex scrape of the model's
    prose): tracks brace depth while respecting JSON string-literal escaping
    so a ``}``/``{`` inside a quoted string never perturbs the count. Returns
    the raw source of the first block whose depth returns to zero, or
    :data:`None` when no top-level ``{`` exists or none ever balances. The
    caller still requires this candidate to round-trip through
    :func:`_contract_object_from_candidate` -- this only locates WHERE a JSON
    object starts and ends, never what it means.
    """

    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def parse_agent_reply(text: str) -> dict[str, Any] | None:
    """Parse the agent's reply into ``{"answer": {...}}`` / ``{"decline": ...}``.

    STRUCTURAL JSON parsing only -- never a keyword/phrase scrape of the
    model's prose (superseding principle #1: clio never fabricates a decision
    from a model's free text). The model was explicitly instructed to reply
    with exactly one JSON object; this either parses that declared contract or
    it does not. Tolerates two structural (never semantic) shapes: a ```json
    fenced block, and -- since the caller now reads the answer turn's own
    ``answer``-field text specifically (:func:`answer_field_text`), which
    SHOULD already be bare JSON, but a model can still preface it with a
    stray word or two despite the explicit instruction not to -- the first
    balanced top-level ``{...}`` block anywhere in the text
    (:func:`_first_balanced_json_object`). Both paths still require the SAME
    strict ``json.loads`` + declared answer/decline Mapping contract; an
    unparseable/ambiguous reply is :data:`None`, which the caller treats as a
    typed fallback, never a guess.
    """

    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    if not candidate:
        return None
    parsed = _contract_object_from_candidate(candidate)
    if parsed is not None:
        return parsed
    block = _first_balanced_json_object(candidate)
    if block is None:
        return None
    return _contract_object_from_candidate(block)
