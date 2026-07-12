"""clio ReActV2 subclass — dormant infrastructure behind the kill-switch (#901).

This is clio's subclass of dspy's (experimental) ``ReActV2`` plus the two seams
that make its append-only ``dspy.History`` composition ride clio's ARC live plane
and frozen wire contract. Everything here is reached only when the OFF-by-default
kill-switch (``_reactv2_enabled`` in :mod:`clio_agent.gact.agents.runtime`) selects
the V2 class; the classic ``_RetainingReAct`` stays the production path until parity
is proven (design ``901_reactv2_design.md`` slices S2–S6).

**Why V2 at all** (design §1–3): ReActV2 composes each turn as an append-only
``dspy.History`` of structured messages instead of one ever-growing re-rendered
``trajectory`` string, so a provider's prompt-cache sees a byte-stable prefix across
``self.react`` calls (the #891 payoff). clio's own ARC compact/delete op becomes the
*sole* prefix-reset author (V2 has no ``truncate_trajectory``).

**S1 — the frozen-contract signature defense** (§4, risk 1): clio's ReAct-internal
``next_thought`` field is typed plain ``str``, NOT ``dspy.Reasoning``.
``dspy.Reasoning.adapt_to_native_lm_feature`` deletes the Reasoning field from the
signature and sets ``reasoning_effort`` on any reasoning-capable model, which would
route ``next_thought`` onto the provider's *native reasoning channel* (clio's
thinking lane) instead of the visible ``[[ ## next_thought ## ]]`` response lane —
inverting the frozen wire contract (thinking = provider CoT ONLY; next_thought =
response). Typing it ``str`` keeps it a text-rendered field and leaves the #877
marker-split path unchanged.

**S2 — the ARC fold seam** (§5, design B): :func:`segments_to_messages` folds the
MATERIALIZED ARC live plane (the ordered thought/tool_call/observation segments the
loop wrote and the ARC ops mutate) into the ``dspy.History`` message list ReActV2
consumes. The read seam is a custom adapter override of ``format_conversation_history``
(:func:`override_history_inputs_from_arc`, wired from
:class:`clio_agent.lm.adapters.LenientChatAdapter`): the analog of the classic
``_format_trajectory`` override. It sources straight from ARC's materialized render,
so ARC stays the single wire source and an out-of-band ARC edit (delete/summarize/
insert/append) changes the next prompt. **Binding owner condition:** the fold reads
the materialized plane (``ARCMemory.render_segments`` → ``SegmentStore.render``),
NEVER re-derives context from the canonical semantic-event log per request; ARC ops
(compact/delete) remain the sole reset authors of the History prefix.

**S3 — the #878 contract rework onto submit** (§4, risk 2): ReActV2 has no ``extract``
step — the final outputs ride the internal ``submit`` tool's typed args and flow to
the returned ``Prediction`` unchanged. The classic #878 extract-suppression (the tap
gate ``react_extract_field_suppressed``) has nothing to fire on, so this class
re-expresses its intent on the submit turn: :meth:`_RetainingReActV2._execute_tool_calls`
records a typed ``react_submit_field_suppressed`` reason for every submit output field
(its VALUE flows to the return contract, it is NOT emitted as visible text — in V2 the
submit args ride the ``tool_calls`` field, never a visible ``answer``/``reasoning``
lane) and a ``react_submit_invalid_output`` reason when the ``submit`` tool rejects a
typed/missing output arg (a degraded path — the value did NOT flow). Both are recorded
through the stream-audit sink in the ``stream_fallback`` house style so no routing is
silent. The classic path's #878 handling is byte-untouched (it lives in
``lm_activity``/``streaming`` and is never reached from V2).

**Import placement**: ``dspy`` is imported at module scope on purpose. This module is
itself *deferred* — nothing imports it at package load; only
:func:`retaining_reactv2_cls` (reached from ``runtime._retaining_react_cls`` when the
kill-switch is ON) or the ``LenientChatAdapter`` read seam or a test pulls it in.
Every ARC / gact-context / audit import is lazy inside the functions that need it, so
the class stays a real, importable, testable module-scope unit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import dspy
from dspy.adapters.types.tool import ToolCallResults, ToolCalls

if TYPE_CHECKING:  # pragma: no cover - typing only
    from clio_agent.arc.schema import Segment

# ---- typed reason catalog (no-silent-fallback, stream_fallback house style) ----
#
# Each is a queryable ``duplicate_reason`` recorded on the ``stream_audit`` sink so a
# routing / degradation decision on the V2 path is never silent (see the module
# docstring, S3, and the #772 no-silent-fallback rule).

# A submit output field's VALUE flowed to the return contract (the final Prediction /
# the delegation ``output`` behind *show more*) and was NOT emitted as visible text.
REACT_SUBMIT_FIELD_SUPPRESSED = "react_submit_field_suppressed"
# The ``submit`` tool rejected a typed/missing final-output arg — a degraded turn: the
# value did NOT flow to the return contract (the loop force-submits / ends instead).
REACT_SUBMIT_INVALID_OUTPUT = "react_submit_invalid_output"
# The ARC materialized-plane read raised while folding the History prefix; the seam
# fell back to ReActV2's own internal append-only history for this call.
REACTV2_ARC_HISTORY_READ_FAILED = "reactv2_arc_history_read_failed"


class _RetainingReActV2(dspy.ReActV2):  # type: ignore[misc, name-defined]
    """clio subclass of dspy's experimental ``ReActV2`` (see module docstring)."""

    def _make_react_signature(self) -> type[dspy.Signature]:
        """Build the ReAct-internal predict signature, retyping ``next_thought``.

        Mirrors ``dspy.predict.react_v2.ReActV2._make_react_signature`` and then
        applies the single frozen-contract change: ``next_thought`` becomes plain
        ``str`` instead of ``dspy.Reasoning`` (see the class/module docstring). Using
        the public :meth:`dspy.Signature.with_updated_fields` — rather than
        re-authoring the whole method body — keeps clio in lockstep with upstream:
        only the field *type* is overridden, every other field, instruction, and
        ordering flows through unchanged. A unit test guards the resulting field
        types / signature shape.
        """
        signature = super()._make_react_signature()
        return signature.with_updated_fields("next_thought", type_=str)

    def _execute_tool_calls(
        self, tool_calls: ToolCalls
    ) -> tuple[ToolCallResults, dict[str, Any] | None]:
        """Execute the step's tool calls, then audit the ``submit`` turn (#901 S3).

        Delegates verbatim to ``ReActV2._execute_tool_calls`` (the values / final
        outputs are produced exactly as upstream), then records the typed submit-turn
        reasons that re-express #878's intent on V2's submit path. This is the single
        chokepoint the ``submit`` tool runs through for BOTH the normal loop
        (``forward``) and the forced tail (``_forced_submit``), so the audit fires
        wherever a final output is produced — no forward mirror is needed.
        """
        results, final_outputs = super()._execute_tool_calls(tool_calls)
        self._audit_submit_tool_calls(tool_calls, results, final_outputs)
        return results, final_outputs

    def _audit_submit_tool_calls(
        self,
        tool_calls: ToolCalls,
        results: ToolCallResults,
        final_outputs: dict[str, Any] | None,
    ) -> None:
        """Record the typed submit-turn reasons (#901 S3), never silently.

        For the ``submit`` call in this step:

        * on success (``final_outputs`` produced), record one
          ``react_submit_field_suppressed`` per final-output field — its VALUE rides
          the returned ``Prediction`` (the parent's return contract, rendered behind
          *show more*) and is NOT emitted as a visible text lane (V2 submit args ride
          the ``tool_calls`` field, so there is no ``answer``/``reasoning`` visible
          copy to suppress — the reason is the record that the value was routed);
        * on an error result (the ``submit`` tool rejected a typed/missing output
          arg), record ``react_submit_invalid_output`` with the rejection text — a
          degraded turn where the value did NOT flow.

        Best-effort: auditing must never break the loop. The stream-audit sink is a
        no-op unless configured, so this is free on the hot path.
        """
        result_by_id = {r.call_id: r for r in results.tool_call_results if r.call_id is not None}
        agent_id = _active_react_scope_safe()
        for call in tool_calls.tool_calls:
            if call.name != "submit":
                continue
            result = result_by_id.get(call.id)
            if result is not None and result.is_error:
                _record_submit_audit(
                    REACT_SUBMIT_INVALID_OUTPUT,
                    agent_id=agent_id,
                    field="submit",
                    text=str(result.value),
                    suppressed=False,
                )
                continue
            if final_outputs is None:
                continue
            for field, value in final_outputs.items():
                _record_submit_audit(
                    REACT_SUBMIT_FIELD_SUPPRESSED,
                    agent_id=agent_id,
                    field=field,
                    text=_stringify_value(value),
                    suppressed=True,
                )


def retaining_reactv2_cls() -> type[Any]:
    """Return the clio ReActV2 subclass — the V2 leg of ``_retaining_react_cls``.

    Parallels :func:`clio_agent.gact.agents.runtime._retaining_react_cls`'s classic
    factory so the kill-switch is a single branch. The class is a module-level
    constant (no per-base cache is needed: unlike the classic path, clio binds
    directly to the concrete ``dspy.ReActV2`` here rather than a test-monkeypatched
    ``dspy.ReAct``).
    """
    return _RetainingReActV2


# ----------------------------------------------------------------------------- #
# S2 — the ARC fold seam: materialized live plane -> dspy.History messages       #
# ----------------------------------------------------------------------------- #


def segments_to_messages(segments: list[Segment]) -> list[dict[str, Any]]:
    """Fold ordered LIVE ARC segments into ReActV2's ``dspy.History`` message list.

    The V2 analog of :func:`clio_agent.arc.segments.segments_to_keys`. It reuses that
    exact trajectory-dict projection (imported, not re-implemented, so the turn
    grouping / gapless re-indexing is byte-identical to the classic path and
    segments.py is not regrown against its size ratchet), then maps each recomputed
    turn ``i`` into one ``ReActV2`` history event:

    * ``thought_i``          -> ``event["next_thought"] = <text>`` (str, the response
      lane — never ``dspy.Reasoning``, matching the S1 signature defense);
    * ``tool_name_i`` +
      ``tool_args_i``        -> ``event["tool_calls"] = ToolCalls([ToolCall(id=
      "call_i_0", name, args)])`` with, when ``observation_i`` is present, the
      observation merged in as the tool call's ``ToolCallResults`` (``id`` scheme
      matches ``ReActV2._ensure_tool_call_ids`` so the folded events line up with a
      stock V2 loop);
    * a lone ``observation_i`` (e.g. a compaction ``summary`` segment, which
      ``segments_to_keys`` renders as an ``observation`` with no tool call) ->
      ``event["next_thought"] = <text>`` so its content still reaches the wire.

    Pure and side-effect-free. Robust to malformed segment content: a missing text is
    ``""`` and a non-dict ``args`` is coerced to ``{}`` so a bad write can never raise
    here (the "wrong-input" path). The current user input is intentionally NOT folded
    into the history prefix — like the classic trajectory it is rendered separately by
    the adapter's current-input path each call, keeping the cached prefix free of the
    duplicated question (a documented deviation from stock V2, which embeds the input
    in the first history event; the byte-equality reference is regenerated in S5).

    Args:
        segments: Ordered LIVE segments (``ARCMemory.render_segments`` output).

    Returns:
        A list of history-event dicts keyed by react-signature field names.
    """
    from clio_agent.arc.segments import segments_to_keys  # noqa: PLC0415

    keys = segments_to_keys(segments)
    turn_count = _turn_count(keys)
    messages: list[dict[str, Any]] = []
    for i in range(turn_count):
        event: dict[str, Any] = {}
        if f"thought_{i}" in keys:
            event["next_thought"] = _as_text(keys[f"thought_{i}"])
        if f"tool_name_{i}" in keys:
            args = keys.get(f"tool_args_{i}")
            call = ToolCalls.ToolCall(
                id=f"call_{i}_0",
                name=_as_text(keys[f"tool_name_{i}"]),
                args=args if isinstance(args, dict) else {},
            )
            tool_calls = ToolCalls(tool_calls=[call])
            if f"observation_{i}" in keys:
                tool_calls = tool_calls.model_copy(
                    update={
                        "tool_call_results": ToolCallResults.from_tool_calls_and_values(
                            tool_calls, [keys[f"observation_{i}"]], [False]
                        )
                    }
                )
            event["tool_calls"] = tool_calls
        elif f"observation_{i}" in keys:
            # A summary / orphan observation (no owning tool call) — surface its text
            # so a compaction reset still reaches the wire.
            event["next_thought"] = _as_text(keys[f"observation_{i}"])
        if event:
            messages.append(event)
    return messages


def arc_history_messages() -> list[dict[str, Any]] | None:
    """Fold the active scope's MATERIALIZED ARC live plane into History messages.

    Resolves the ARC handle + ``(session, scope)`` from the runtime context — the
    same seam the classic ``_RetainingReAct._format_trajectory`` reads — and folds
    ``ARCMemory.render_segments`` (the materialized ``SegmentStore.render``, NEVER a
    re-derivation from the canonical semantic-event log) through
    :func:`segments_to_messages`.

    Returns ``None`` — meaning "ARC is not the source for this call; use ReActV2's own
    internal append-only history" — when ARC is disabled, no react scope is active, or
    the materialized plane is empty (so a wired end-to-end V2 turn is never handed an
    empty prefix that would wipe its in-flight history). Returns the folded message
    list once the plane holds a working set, so an out-of-band ARC edit propagates to
    the next prompt. A read failure is recorded as a typed reason and also returns
    ``None`` (no silent fallback).
    """
    from clio_agent.gact import context as _ctx  # noqa: PLC0415

    app = _ctx.active_app()
    scope = _ctx.active_react_scope()
    session = _ctx.active_react_session()
    arc = getattr(getattr(app, "state", None), "arc", None) if (app is not None and scope) else None
    if arc is None or not scope:
        return None
    try:
        segments = arc.render_segments(session, scope)
        messages = segments_to_messages(segments)
    except Exception as exc:  # noqa: BLE001 - documented no-silent-fallback: record + fall back
        _record_submit_audit(
            REACTV2_ARC_HISTORY_READ_FAILED,
            agent_id=scope,
            field="history",
            text=str(exc),
            suppressed=False,
        )
        return None
    return messages or None


def override_history_inputs_from_arc(inputs: dict[str, Any], history_field_name: str) -> bool:
    """Point ReActV2's History input at the materialized ARC live plane (S2 read seam).

    The adapter-side half of design B. Called from
    :meth:`clio_agent.lm.adapters.LenientChatAdapter.format_conversation_history`
    *before* it delegates to the stock formatter: when the active scope's ARC plane
    holds a working set, this replaces ``inputs[history_field_name]`` in place with a
    fresh ``dspy.History`` folded from it, so the stock formatter renders ARC's
    materialized state byte-for-byte and ARC stays the single wire source. When ARC is
    not the source (disabled / no scope / empty / read failure — see
    :func:`arc_history_messages`) it is a no-op and the passed-in History is rendered
    unchanged.

    Only fires for a signature that actually carries a ``dspy.History`` input field —
    in clio that is exclusively the ReActV2 react signature — so the classic
    (History-less) wire path can never reach this branch and stays byte-identical.

    Args:
        inputs: The adapter's mutable per-call inputs copy (mutated in place).
        history_field_name: The signature's ``dspy.History`` input field name.

    Returns:
        ``True`` when the History input was sourced from ARC, else ``False``.
    """
    if not history_field_name or history_field_name not in inputs:
        return False
    messages = arc_history_messages()
    if messages is None:
        return False
    inputs[history_field_name] = dspy.History(messages=messages)
    return True


# ---- small helpers ---------------------------------------------------------- #


def _turn_count(keys: dict[str, Any]) -> int:
    """Number of react turns in a ``segments_to_keys`` dict (max index + 1).

    ``segments_to_keys`` re-indexes gaplessly from 0, so the count is one past the
    largest ``*_<i>`` suffix; ``{}`` -> 0.
    """
    highest = -1
    for key in keys:
        _, _, suffix = key.rpartition("_")
        if suffix.isdigit():
            highest = max(highest, int(suffix))
    return highest + 1


def _as_text(value: Any) -> str:
    """Coerce a folded field value to ``str`` (a bad write must never raise here)."""
    return value if isinstance(value, str) else str(value if value is not None else "")


def _stringify_value(value: Any) -> str:
    """Compact string form of a submit output value for the audit record."""
    if isinstance(value, str):
        return value
    try:
        import json  # noqa: PLC0415

        return json.dumps(value, default=str)
    except Exception:  # noqa: BLE001 - audit text only; never fail a turn on formatting
        return str(value)


def _active_react_scope_safe() -> str:
    """The active react scope for audit attribution, or ``""`` off-turn."""
    try:
        from clio_agent.gact import context as _ctx  # noqa: PLC0415

        return _ctx.active_react_scope()
    except Exception:  # noqa: BLE001 - scope unavailable off-turn (CLI / optimizer / unit test)
        return ""


def _record_submit_audit(
    reason: str,
    *,
    agent_id: str,
    field: str,
    text: str,
    suppressed: bool,
) -> None:
    """Emit one V2-path stream-audit record (the no-silent-fallback house style).

    Mirrors ``lm_activity.note_suppressed_extract_field`` (the classic #878 record) so
    the V2 submit-turn reasons are queryable in the same ``bridge.contract_field``
    lane. The sink is a no-op unless ``CLIO_STREAM_AUDIT_LOG`` is configured.
    """
    from clio_agent.runtime.stream_audit import stream_audit  # noqa: PLC0415

    stream_audit(
        "bridge.contract_field",
        agent_id=agent_id or "",
        field=field,
        chunk_len=len(text),
        visible=False,
        duplicate_suppressed=suppressed,
        duplicate_reason=reason,
        head=text[:120],
        full_text=text[:12000],
    )
