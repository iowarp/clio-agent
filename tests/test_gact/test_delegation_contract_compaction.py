from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from clio_agent.gact.app import (
    _dynamic_parent_resume_prompt,
    _expert_handoff_fields,
    _latest_parent_resumed_output_summary,
)
from clio_agent.gact.return_summary import (
    _looks_like_structured_answer,
    _render_return_summary,
    public_return_summary,
)
from clio_agent.gact.types import Part
from tests.test_gact.earthscope_schema import EARTHSCOPE_WORKFLOW_STATE_SCHEMA


def _is_bare_json_body(text: str) -> bool:
    """True when the WHOLE trimmed body parses as a JSON object/array.

    This is exactly the shape the retired web-client ``dropBareJsonSummary`` scrub
    used to drop from a handoff ``output_summary``. The server guarantee below is
    that this never happens on the completed-delegation emit path.
    """

    stripped = text.strip()
    if not stripped:
        return False
    wrapped = (stripped.startswith("{") and stripped.endswith("}")) or (
        stripped.startswith("[") and stripped.endswith("]")
    )
    if not wrapped:
        return False
    try:
        json.loads(stripped)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class _Agent:
    id: str


def test_expert_handoff_part_carries_structured_fields_from_row() -> None:
    """An expert_handoff Part exposes the delegation as typed fields (parent/child/
    stage/status) drawn from the structured row, so a client never parses the prose
    ``text`` label to attribute the handoff."""

    row = {
        "agent_id": "geospatial",  # the child that received the delegation
        "parent_id": "main",  # the parent that made it
        "stage": "delegate.completed",
        "status": "completed",
        "output_summary": "staged waveform",
    }
    fields = _expert_handoff_fields(row)
    assert fields == {
        "parent_agent": "main",
        "child_agent": "geospatial",
        "stage": "delegate.completed",
        "status": "completed",
    }

    part = Part(
        type="expert_handoff",
        agent_id=fields["parent_agent"],
        parent_agent=fields["parent_agent"],
        child_agent=fields["child_agent"],
        stage=fields["stage"],
        status=fields["status"],
        text="",  # the UI consumes the structured fields, not the string
    )
    # The handoff is fully described without the prose ``text``.
    assert part.text == ""
    assert part.parent_agent == "main"
    assert part.child_agent == "geospatial"
    assert part.stage == "delegate.completed"
    assert part.status == "completed"
    # The generating party is the parent.
    assert part.agent_id == "main"


def test_parent_resume_prompt_receives_genuine_child_output_verbatim() -> None:
    # The child's GENUINE output flows to the parent verbatim — no heuristic
    # compaction/truncation. A long output is NOT trimmed, and no truncation
    # scaffolding marker is injected.
    child_output = "\n".join(
        [
            "Analysis completed from fresh SAC evidence.",
            "NEXT_EXPERT: visualization",
            "NEXT_ACTION: plot_sac_traces /tmp/fresh.sac",
            "DO_NOT_FINALIZE_BEFORE_VISUALIZATION: true",
            *[f"long note {idx}" for idx in range(180)],
        ]
    )

    prompt = _dynamic_parent_resume_prompt(
        "Recover waveform evidence and produce a PNG artifact.",
        _Agent(id="main"),  # type: ignore[arg-type]
        [
            {
                "stage": "delegate.completed",
                "agent_id": "analysis",
                "status": "completed",
                "output": child_output,
            }
        ],
        schema=EARTHSCOPE_WORKFLOW_STATE_SCHEMA,
    )

    assert "NEXT_EXPERT: visualization" in prompt
    assert "NEXT_ACTION: plot_sac_traces /tmp/fresh.sac" in prompt
    assert "DO_NOT_FINALIZE_BEFORE_VISUALIZATION: true" in prompt
    # Full output flows: even the last long note survives, and nothing is truncated.
    assert "long note 179" in prompt
    assert "truncated" not in prompt


def test_latest_parent_resumed_output_summary_prefers_final_nested_parent_result() -> None:
    rows = [
        {
            "stage": "delegate.completed",
            "agent_id": "per_sample_metrics",
            "parent_id": "cohort_qc",
            "output": "Initial metrics child result.",
        },
        {
            "stage": "parent.resumed",
            "agent_id": "cohort_qc",
            "resumed_from": "per_sample_metrics",
            "output": "Metrics summarized by coordinator.",
        },
        {
            "stage": "delegate.completed",
            "agent_id": "manifest_reconciliation",
            "parent_id": "cohort_qc",
            "output": "No manifest was provided.",
        },
        {
            "stage": "parent.resumed",
            "agent_id": "cohort_qc",
            "resumed_from": "manifest_reconciliation",
            "output": "Final coordinator answer with metrics and manifest caveat.",
        },
    ]

    assert (
        _latest_parent_resumed_output_summary(rows, "cohort_qc")
        == "Final coordinator answer with metrics and manifest caveat."
    )


def _full_public_summary(raw: str) -> str:
    """Build a child answer's public ``output_summary`` exactly as the server does.

    This calls the SEAM (``public_return_summary``) — the single owner the
    completed-delegation emit path (``turn_delegation.py`` completed row) now calls.
    The invariant is a property of the COMPOSITION (render + transcript clean), so
    asserting against the seam — not either inner function alone — is what proves
    the value the web client receives is never a bare JSON body. Asserting on the
    inner ``_render_return_summary`` alone (as an earlier test did) passed while the
    composed invariant was still false, because a marker-prefixed JSON body only
    becomes bare AFTER the cleaner strips the marker.
    """

    return public_return_summary(raw, schema=EARTHSCOPE_WORKFLOW_STATE_SCHEMA)


def test_render_return_summary_never_emits_a_bare_json_body() -> None:
    """The completed delegation's public ``output_summary`` is derived from
    ``_render_return_summary`` (see ``turn_delegation.py`` completed path); it must
    NEVER be a bare JSON body.

    This is the server-side guarantee that lets the web client render the handoff
    summary VERBATIM (epic #880) with no ``dropBareJsonSummary`` scrub: a structured
    answer is rendered to a readable one-liner (a ``summary``/``description``/``answer``
    /``result`` field, ``key: value`` scalars, or ``N item(s)``) or to ``""`` — never
    passed through as raw JSON. The table below includes the DOUBLE-ENCODED shapes
    (a field whose value is itself a JSON string) that the retired client scrub used
    to catch, asserted against the FULL public path.
    """

    inner_region = json.dumps({"region": "San Diego", "lat": 32.7})
    inner_nested = json.dumps({"result": json.dumps({"ok": True, "count": 2})})
    table = [
        # Already-working shapes (must not regress).
        json.dumps({"REGION_LABEL": "San Diego area", "CENTER_LAT": 32.7, "RADIUS_KM": 50}),
        json.dumps({"summary": "Resolved the region to San Diego."}),
        json.dumps({"geospatial": {"status": "resolved", "region_name": "San Diego"}}),
        json.dumps([{"claim": "supported"}, {"claim": "refuted"}]),
        "```json\n" + json.dumps({"answer": "done", "count": 3}) + "\n```",
        json.dumps({}),  # structurally empty -> ""
        # Proven-failing shapes: a double-encoded answer under each priority field.
        json.dumps({"answer": inner_region}),
        json.dumps({"summary": "[1, 2, 3]"}),  # a JSON ARRAY string
        json.dumps({"description": inner_region}),
        json.dumps({"result": json.dumps({"ok": True})}),
        # A JSON string nested under a single-key namespace wrapper.
        json.dumps({"geospatial": {"answer": inner_region}}),
        # A fenced ```json body whose answer field is itself double-encoded.
        "```json\n" + json.dumps({"answer": inner_region}) + "\n```",
        # Deeply nested double encoding (answer -> result -> object).
        json.dumps({"answer": inner_nested}),
        # COMPOSED-LEAK counterexamples (the #877 SDK thinking/contract-split
        # re-emits a ``[[ ## field ## ]]`` marker mid-field). Verbatim, proven to
        # leak on the COMPOSED path pre-fix: _render_return_summary's json.loads
        # rejects the marker prefix and passes the body through, then the transcript
        # cleaner strips the marker and UNMASKS a bare JSON body. The seam catches it.
        '[[ ## answer ## ]]{"region": "San Diego"}',
        "[[ ## x ## ]][1, 2, 3]",
        '[[ ## next_thought ## ]] [[ ## answer ## ]]{"lat": 32.7, "lon": -117.1}',
        # A marker leaked INTO a field value one level down (nested composed leak).
        json.dumps({"summary": '[[ ## answer ## ]]{"a": 1}'}),
    ]
    for raw in table:
        assert _looks_like_structured_answer(raw), raw
        rendered = _render_return_summary(raw)
        assert not _is_bare_json_body(rendered), (
            f"_render_return_summary leaked a bare JSON body for {raw!r}: {rendered!r}"
        )
        # The FULL public path (the seam: render + transcript clean, invariant
        # enforced) must also be JSON-free.
        assert not _is_bare_json_body(_full_public_summary(raw)), (
            f"public output_summary leaked a bare JSON body for {raw!r}: "
            f"{_full_public_summary(raw)!r}"
        )


def test_render_return_summary_property_never_yields_a_bare_json_body() -> None:
    """Generative invariant: for the CROSS-PRODUCT of body shapes × leaked-marker
    prefixes, ``json.loads`` of the FULL public summary (the seam) NEVER yields a
    dict/list. This ENFORCES the universal guarantee (not a handful of spot checks):
    a bare JSON body can never reach the web client on the completed-delegation path.

    Body shapes: plain object/array, single-encoded, double-encoded, triple-encoded,
    array-string, fenced ```json, and namespace-wrapped. Marker prefixes (the #877
    SDK thinking/contract-split re-emission): none, one ``[[ ## field ## ]]``, two
    markers, and one marker with leading whitespace. The prefixed cases are exactly
    the COMPOSED leak: the marker survives ``_render_return_summary`` verbatim, then
    the transcript cleaner strips it and UNMASKS the JSON underneath — which the seam
    must re-summarise.
    """

    payloads: list[Any] = [
        {"region": "San Diego", "lat": 32.7},
        {"ok": True, "count": 2},
        [1, 2, 3],
        [{"claim": "supported"}],
        {},
        "plain scalar answer",
    ]
    wrappers = ("summary", "description", "answer", "result")

    base_bodies: list[str] = []
    for payload in payloads:
        encoded = json.dumps(payload)
        # The raw payload, and the payload double/triple-encoded under each wrapper.
        if isinstance(payload, (dict, list)):
            base_bodies.append(encoded)
        for key in wrappers:
            single = json.dumps({key: encoded})  # value is a JSON string (double)
            base_bodies.append(single)
            base_bodies.append(json.dumps({"geospatial": {key: encoded}}))  # namespaced
            base_bodies.append(json.dumps({key: single}))  # triple-encoded
            base_bodies.append("```json\n" + single + "\n```")  # fenced double

    # Cross the body shapes with leaked-marker prefixes: {none, one, two, ws+one}.
    marker_prefixes = (
        "",
        "[[ ## answer ## ]]",
        "[[ ## next_thought ## ]] [[ ## answer ## ]]",
        "\n  [[ ## answer ## ]]",
    )
    bodies: list[str] = [prefix + body for body in base_bodies for prefix in marker_prefixes]

    checked = 0
    for body in bodies:
        # The seam is the value the server stores; assert on it. (The raw
        # _render_return_summary alone is NOT asserted here — a marker-prefixed body
        # is intentionally passed through verbatim by it; only the composed seam is
        # required to be JSON-free.)
        candidate = _full_public_summary(body)
        stripped = candidate.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except ValueError:
            continue  # not JSON at all -> trivially not a bare JSON body
        assert not isinstance(parsed, (dict, list)), (
            f"summary is a bare JSON body for input {body!r}: {candidate!r}"
        )
        checked += 1
    # Guard the guard: the loop must actually have exercised renderable outputs.
    assert checked > 0


def test_render_return_summary_passes_prose_through_verbatim() -> None:
    """Prose answers (the common completed-delegation deliverable) flow verbatim.

    This is the render intent the retired client scrub must never have altered: a
    real prose summary is returned unchanged, never treated as machine state.
    """

    prose = "Staged GNSS CSV for station P472 near the requested center."
    assert not _looks_like_structured_answer(prose)
    assert _render_return_summary(prose) == prose
    # The seam must not alter a prose deliverable either.
    assert _full_public_summary(prose) == prose


def test_public_return_summary_renders_readable_positive_outputs() -> None:
    """POSITIVE guarantees (a negative-only invariant lets a future edit silently
    ruin the output): the seam renders real content, not just "not-bare-JSON".

    * A prose answer passes through verbatim.
    * A scalar-field object renders ``key: value`` pairs.
    * A JSON array renders ``N item(s)``.
    * A structurally-empty object yields ``""`` (the readable-fallback signal the
      caller replaces with ``"<child> returned to <parent>."``).
    * A marker-prefixed scalar object still renders ``key: value`` (composed leak).
    """

    # Prose: verbatim.
    assert (
        _full_public_summary("Resolved the region to San Diego.")
        == "Resolved the region to San Diego."
    )
    # Scalar object: key: value pairs.
    assert (
        _full_public_summary(json.dumps({"region": "San Diego", "lat": 32.7}))
        == "region: San Diego; lat: 32.7"
    )
    # List: N item(s).
    assert _full_public_summary(json.dumps([1, 2, 3])) == "3 item(s)"
    assert _full_public_summary(json.dumps([{"claim": "supported"}])) == "1 item(s)"
    # Structurally empty: the readable-fallback signal.
    assert _full_public_summary(json.dumps({})) == ""
    # Marker-prefixed scalar object: unmasked then summarised, not leaked.
    assert _full_public_summary('[[ ## answer ## ]]{"region": "San Diego"}') == "region: San Diego"


def test_public_return_summary_bounded_on_deeply_nested_input() -> None:
    """Bounded recursion: a pathologically deep double-encoded body must terminate
    (no unbounded recursion / stack overflow) and still honor the invariant.

    ``_render_return_summary`` caps its double-encoding unwrap at ``_MAX_RENDER_DEPTH``
    and the seam caps its re-summarise passes at ``_MAX_SEAM_PASSES``; a body nested
    far past both must simply resolve to a non-bare summary (or ``""``), never hang.

    Depth 12 clears both caps (3 and 4) while keeping the input small: each
    ``json.dumps`` re-escapes the prior level's backslashes, so string size grows
    ~2**depth — a deeper value would blow up the *test input* itself, not the code.
    """

    body: str = json.dumps({"scalar_here": "leaf value"})
    for _ in range(12):
        body = json.dumps({"answer": body})  # answer -> answer -> ... -> {scalar}
    # Also stress the composed path: prepend a leaked marker at the top.
    marker_body = "[[ ## answer ## ]]" + body

    for candidate in (_full_public_summary(body), _full_public_summary(marker_body)):
        assert not _is_bare_json_body(candidate), candidate


# JavaScript's String.prototype.trim() whitespace set (ECMAScript WhiteSpace +
# LineTerminator). It includes U+FEFF/U+200B/U+2060, which Python's str.strip()
# leaves in place — the exact asymmetry that let a BOM-prefixed JSON body look
# "not bare" to the server while the browser trimmed and parsed it.
_JS_TRIM_CHARS = "\t\n\v\f\r                  　﻿​⁠"


def _js_would_parse_as_bare_json(text: str) -> bool:
    """Adjudicate the body the way the WEB CLIENT would.

    Deliberately does NOT call the server's ``_is_bare_json_body``: asserting the
    server's guarantee with the server's own predicate is circular, and it was that
    predicate which was wrong (Python ``strip`` vs JS ``trim``; ``RecursionError``
    swallowed as "safe"). V8's ``JSON.parse`` is iterative, so deep nesting parses.
    """

    trimmed = text.strip(_JS_TRIM_CHARS)
    wrapped = (trimmed.startswith("{") and trimmed.endswith("}")) or (
        trimmed.startswith("[") and trimmed.endswith("]")
    )
    if not wrapped:
        return False
    try:
        return isinstance(json.loads(trimmed), (dict, list))
    except RecursionError:
        return True  # V8 would have parsed it
    except json.JSONDecodeError:
        return False


def test_public_return_summary_never_bare_json_under_js_trim_semantics() -> None:
    """A zero-width/BOM-padded JSON body must not reach the client as bare JSON.

    Regression: ``\ufeff{"region": "San Diego"}`` passed the server's
    ``str.strip()``-based check (BOM is not Python whitespace) and was stored
    verbatim; the browser's ``trim()`` removed the BOM and ``JSON.parse`` succeeded,
    rendering a raw JSON blob where the child's answer belonged.
    """

    payload = json.dumps({"region": "San Diego"})
    for pad in ("﻿", "​", "⁠", "﻿\n "):
        for candidate in (f"{pad}{payload}", f"{payload}{pad}", f"{pad}{payload}{pad}"):
            summary = _full_public_summary(candidate)
            assert not _js_would_parse_as_bare_json(summary), (candidate, summary)
    # The content survives the transform — this is a re-render, not a drop.
    assert "San Diego" in _full_public_summary(f"﻿{payload}")


def test_public_return_summary_never_bare_json_for_deeply_nested_input() -> None:
    """Python's recursive ``json.loads`` raises ``RecursionError`` where V8's
    iterative ``JSON.parse`` succeeds. Treating "Python could not parse it" as
    "the client cannot parse it" is backwards; such a body must never pass through.
    """

    deep = "[" * 2000 + "]" * 2000
    summary = _full_public_summary(deep)
    assert not _js_would_parse_as_bare_json(summary), summary
