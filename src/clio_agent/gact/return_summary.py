"""Structured-answer rendering for delegation return summaries (owner module).

A completed child delegation's public ``output_summary`` is derived here from the
child's GENUINE answer (``turn_delegation.py`` completed path). Prose answers pass
through verbatim; structured (JSON) answers — the typed ``dspy.extract``
deliverable — are rendered into a compact, grounded one-liner so the transcript
shows the real result instead of a generic placeholder.

Extracted from ``delegation.py`` (#880 presentation-model epic) so the structural
guarantee below lives in a focused owner module, not appended to a god-file.

THE INVARIANT (defined over the COMPOSED value the server actually stores):
    For every child output ``x``, the value stored as a delegation row's
    ``output_summary`` is NEVER a body that ``json.loads`` accepts as an object
    or array (a "bare JSON body"). The invariant is on the COMPOSED result — not
    on any single function.

:func:`_render_return_summary` alone never returns a bare JSON body, and
:func:`~clio_agent.gact.delegation._clean_public_transcript_text` alone only
strips CLIO contract prose + ``[[ ## field ## ]]`` markers. Each is individually
correct, but their COMPOSITION leaked: a marker-prefixed JSON body
(``'[[ ## answer ## ]]{...}'``, the #877 SDK thinking/contract-split
re-emission) makes ``_render_return_summary``'s ``json.loads`` FAIL, so the body
passes through verbatim; the transcript cleaner then strips the marker, UNMASKING
a bare JSON body. That is why the invariant must be enforced at the SEAM, not
inside either function.

:func:`public_return_summary` is that seam — the single owner that composes
clean+render and, by construction, can never return a bare JSON body. The
completed-delegation path (``turn_delegation.py``) calls ONLY it. This is what
lets the web client render the handoff summary VERBATIM with no client-side
``dropBareJsonSummary`` scrub — the server owns the clean stream (#832).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from clio_agent.gact.workflow_state.schema import WorkflowStateSchema

# Bound the seam's re-summarise passes. Each pass strips at most one unmasked
# marker layer (marker-prefixed body -> cleaner strips marker -> bare body ->
# re-render summarises it); real-world marker nesting is depth 0-1, so this cap
# is essentially never reached. When it is, the seam returns "" rather than leak
# raw JSON (the caller supplies the readable fallback).
_MAX_SEAM_PASSES = 4

# Bound the double-encoding unwrap so a pathologically nested answer cannot recurse
# without limit. Mirrors the 2-iteration namespace-wrapper cap below; real-world
# double-encoding is depth 1-2, so this generous cap is essentially never reached.
# When it is, the render falls through to the current node's scalar fields (which
# can never be a bare JSON body), not to a raw JSON blob.
_MAX_RENDER_DEPTH = 3


# The invariant is "the CLIENT cannot JSON.parse this into an object/array", so the
# trim must match JavaScript's. ECMAScript WhiteSpace includes <ZWNBSP> U+FEFF and
# <ZWSP>-adjacent format chars that Python's str.strip() leaves in place: a BOM-
# prefixed body therefore looks non-bare to Python while the client trims it away
# and parses it. Anchored to both ends, mirroring String.prototype.trim().
_JS_TRIM_RE = re.compile(r"\A[\s﻿​⁠]+|[\s﻿​⁠]+\Z")


def _js_trim(text: str) -> str:
    """Trim ``text`` the way JavaScript's ``String.prototype.trim`` would.

    Python's :meth:`str.strip` does not remove ``U+FEFF``/``U+200B``/``U+2060``;
    JavaScript's ``trim()`` does. Any predicate that must agree with the browser
    has to trim on the browser's terms.
    """

    return _JS_TRIM_RE.sub("", text or "")


def _looks_like_structured_answer(text: str) -> bool:
    """True when an expert answer is machine-readable state, not prose."""

    stripped = _js_trim(text)
    if not stripped:
        return False
    return stripped[0] in "{[" or stripped.startswith("```json") or stripped.startswith("```JSON")


def _render_return_summary(output: str, *, _depth: int = 0) -> str:
    """A human-readable one-liner for a child's return, from its GENUINE answer.

    Prose answers pass through unchanged. Structured (JSON) answers — the typed
    ``dspy.extract`` deliverable — are rendered into a compact, grounded summary
    (a ``summary``/``description`` field if present, else the top-level scalar
    fields) so the transcript shows the real result instead of a generic
    "returned a compact result" placeholder. Returns "" when there is nothing
    meaningful to show (caller supplies the fallback).

    Structural guarantee (format-based, wording-agnostic): the returned value is
    NEVER itself a bare JSON body — a string ``json.loads`` accepts as an object or
    array. A double-encoded answer (a chosen field whose value is ITSELF a JSON
    object/array string) is not passed through verbatim; it is recursed into with
    the SAME summarisation, bounded by :data:`_MAX_RENDER_DEPTH`. This is a
    format-only transform (does this string parse as JSON?) — no decision is keyed
    on the answer's wording — so the completed-delegation ``output_summary`` can
    reach the web client and be rendered verbatim without a client-side scrub.

    Args:
        output: The child's raw answer text (prose or a structured JSON body).
        _depth: Internal recursion depth for the double-encoding unwrap; callers
            leave this at its default.

    Returns:
        A compact one-liner, or "" when nothing renderable remains. The result is
        never a bare JSON object/array body on any return path.
    """

    text = _js_trim(output)
    if not text or not _looks_like_structured_answer(text):
        return text
    body = text
    if body.startswith("```"):
        body = body.strip("`")
        body = body.split("\n", 1)[-1].strip() if "\n" in body else ""
    try:
        data = json.loads(body)
    except RecursionError:
        # Too deeply nested for Python's recursive parser, but V8's iterative
        # JSON.parse would accept it — returning it verbatim would hand the client
        # a bare JSON body. Nothing renderable can be extracted, so render nothing.
        return ""
    except Exception:  # noqa: BLE001 - non-JSON delegation body returned verbatim
        # Structured-looking but not parseable: json.loads rejects it, so it is not
        # a bare JSON body and is safe to return verbatim.
        return text
    if isinstance(data, Mapping):
        node: Mapping[str, Any] = data
        # Unwrap a single-key namespace wrapper (e.g. {"<namespace>": {...}}) so the
        # salient fields one level down are summarised, not just "{namespace}".
        for _ in range(2):
            if len(node) == 1:
                only = next(iter(node.values()))
                if isinstance(only, Mapping):
                    node = only
                    continue
            break
        for key in ("summary", "description", "answer", "result"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                inner = value.strip()
                # A chosen field whose value is ITSELF a JSON object/array — a
                # double-encoded answer — must not pass through as a bare JSON body.
                # Recurse into it (same summarisation), bounded by _MAX_RENDER_DEPTH;
                # on a non-empty render take it, else fall through to this node's
                # scalar fields, which can never be a bare JSON body.
                if _looks_like_structured_answer(inner):
                    if _depth < _MAX_RENDER_DEPTH:
                        nested = _render_return_summary(inner, _depth=_depth + 1)
                        if nested:
                            return nested
                    break
                return inner
        scalars = []
        for key, value in node.items():
            if isinstance(value, bool) or isinstance(value, (str, int, float)):
                text_value = str(value)
                if len(text_value) > 60:
                    text_value = text_value[:57] + "..."
                scalars.append(f"{key}: {text_value}")
            if len(scalars) >= 6:
                break
        if scalars:
            return "; ".join(scalars)
    if isinstance(data, list):
        return f"{len(data)} item(s)"
    # Structured but unrenderable (e.g. empty object): no meaningful one-liner.
    return ""


def _is_bare_json_body(text: str) -> bool:
    """True when ``text`` parses whole as a JSON object or array (a bare JSON body).

    This is the exact shape the invariant forbids for a delegation row's
    ``output_summary`` and the exact shape the retired web-client
    ``dropBareJsonSummary`` scrub used to drop. A format-only test (does the whole
    trimmed body parse as a dict/list?) — no decision is keyed on the body's
    wording (superseding principle #1).

    Args:
        text: The candidate summary string.

    Returns:
        ``True`` iff ``json.loads(text.strip())`` yields a ``dict`` or ``list``.
    """

    stripped = _js_trim(text)
    if not stripped or stripped[0] not in "{[":
        return False
    try:
        return isinstance(json.loads(stripped), (dict, list))
    except RecursionError:
        # Python's json is recursive and blows its stack on deeply nested input;
        # V8's JSON.parse is iterative and accepts it. Treating "Python could not
        # parse it" as "the client cannot parse it" would be exactly backwards, so
        # a body we cannot adjudicate is presumed BARE and gets summarised away.
        return True
    except json.JSONDecodeError:
        return False


def public_return_summary(output: str, *, schema: "WorkflowStateSchema") -> str:
    """Owner seam: build a delegation row's public ``output_summary`` from a child's answer.

    This is the SINGLE place the completed-delegation ``output_summary`` is
    produced (``turn_delegation.py`` completed path calls only this). It composes
    the render (:func:`_render_return_summary`) and the transcript clean
    (:func:`~clio_agent.gact.delegation._clean_public_transcript_text`) in the
    exact order the server used, then ENFORCES the module-level invariant BY
    CONSTRUCTION: if marker-stripping inside the cleaner UNMASKED a bare JSON body
    that ``_render_return_summary`` passed through verbatim (its ``json.loads``
    rejected the marker prefix), the now marker-free body is re-rendered +
    re-cleaned — where it parses and summarises — bounded by
    :data:`_MAX_SEAM_PASSES`.

    Enforcing the invariant here, where the value is actually produced, is the
    only construction that CANNOT be bypassed by a future caller: neither
    ``_render_return_summary`` nor ``_clean_public_transcript_text`` can carry the
    guarantee alone (it is a property of their COMPOSITION), and any code path
    that builds a public ``output_summary`` must route through this function.

    Args:
        output: The child's GENUINE answer text (prose or a structured JSON body,
            possibly carrying a leaked ``[[ ## field ## ]]`` marker prefix).
        schema: The active pack workflow_state schema, forwarded to the transcript
            cleaner so contract prose is stripped against the pack's vocabulary.

    Returns:
        A clean, human-readable one-liner. NEVER a bare JSON body (a string
        ``json.loads`` accepts as an object/array) on any return path — ``""`` is
        returned instead when nothing renderable survives, so the caller supplies
        the readable fallback.
    """

    from clio_agent.gact.delegation import _clean_public_transcript_text  # noqa: PLC0415

    summary = _clean_public_transcript_text(_render_return_summary(output), schema=schema)
    for _ in range(_MAX_SEAM_PASSES):
        if not _is_bare_json_body(summary):
            return summary
        summary = _clean_public_transcript_text(_render_return_summary(summary), schema=schema)
    # A pathological body still parses as JSON after the bounded passes: the
    # invariant is absolute, so drop to "" rather than leak raw JSON. In practice
    # this line is unreachable (one pass suffices) — it is the belt-and-suspenders
    # guarantee that the seam can never return a bare JSON body.
    return "" if _is_bare_json_body(summary) else summary
